# -*- coding: utf-8 -*-
"""坡道标注核查平台

- 标注员上传 CSV（图号 1~60000，标记：有坡道 / 无坡道 / 看不清）。
- SQLite 保存原始标注，同人同图重复上传视为更正。
- 每位标注员维护一份 2 bit/图 的压缩汇总（固定 15000 字节，上限 128 KiB），
  原始标注与汇总在同一事务中生效；更正只更新本人汇总。
- 核查只读汇总：任选两人，仅双方都判读为「有坡道/无坡道」的图参与配对，
  输出四种配对计数与 Cohen's κ，并可下载分歧清单（含双方判读）。
- 上传失败沿用原数据；上传期间仍可核查（WAL）；一次核查的计数与分歧清单
  取自同一已提交状态（单读事务 + 汇总版本号校验）。
"""

import csv
import io
import os
import sqlite3

from flask import (Flask, Response, flash, g, redirect, render_template,
                   request, url_for)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("QC_DB_PATH", os.path.join(BASE_DIR, "data", "qc.db"))

MAX_IMAGE_NO = 60000
SUMMARY_LIMIT = 128 * 1024                      # 每人汇总上限 128 KiB
SUMMARY_BYTES = (MAX_IMAGE_NO * 2 + 7) // 8     # 2 bit/图 → 15000 字节

MARK_HAS = "有坡道"
MARK_NONE = "无坡道"
MARK_UNKNOWN = "看不清"
MARK_TO_CODE = {MARK_HAS: 1, MARK_NONE: 2, MARK_UNKNOWN: 3}
CODE_TO_MARK = {v: k for k, v in MARK_TO_CODE.items()}
VALID_CODES = (MARK_TO_CODE[MARK_HAS], MARK_TO_CODE[MARK_NONE])

SCHEMA = """
CREATE TABLE IF NOT EXISTS annotations (
    annotator  TEXT NOT NULL,
    image_no   INTEGER NOT NULL CHECK (image_no BETWEEN 1 AND 60000),
    mark       INTEGER NOT NULL CHECK (mark IN (1, 2, 3)),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (annotator, image_no)
);
CREATE TABLE IF NOT EXISTS summaries (
    annotator  TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,
    data       BLOB NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 上传体积上限
app.secret_key = os.environ.get("QC_SECRET_KEY", "qc-dev-secret")


# ---------------------------------------------------------------- 数据库

def init_db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        con.execute("PRAGMA journal_mode=WAL")  # 上传期间仍可核查
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def get_db():
    if "db" not in g:
        con = sqlite3.connect(DB_PATH, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        g.db = con
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


init_db()


# ---------------------------------------------------------------- 汇总编解码

def pack_summary(pairs):
    """(image_no, code) 序列 → 定长 2 bit/图 字节串。"""
    data = bytearray(SUMMARY_BYTES)
    for image_no, code in pairs:
        i = image_no - 1
        data[i >> 2] |= (code & 0x3) << ((i & 0x3) * 2)
    return bytes(data)


def unpack_code(data, image_no):
    i = image_no - 1
    return (data[i >> 2] >> ((i & 0x3) * 2)) & 0x3


# ---------------------------------------------------------------- CSV 解析

class UploadError(Exception):
    pass


def parse_csv(text):
    """解析 CSV，返回 {image_no: code}；同图多次出现以最后一行为准。"""
    pairs = {}
    header_checked = False
    for lineno, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        if not row or all(not cell.strip() for cell in row):
            continue
        if not header_checked:
            header_checked = True
            try:
                int(row[0].strip())
            except ValueError:
                continue  # 首行是表头
        if len(row) < 2:
            raise UploadError(f"第 {lineno} 行：至少需要「图号,标记」两列")
        img_text = row[0].strip()
        mark_text = row[1].strip()
        try:
            image_no = int(img_text)
        except ValueError:
            raise UploadError(f"第 {lineno} 行：图号「{img_text}」不是整数")
        if not 1 <= image_no <= MAX_IMAGE_NO:
            raise UploadError(
                f"第 {lineno} 行：图号 {image_no} 超出 1～{MAX_IMAGE_NO} 范围")
        if mark_text not in MARK_TO_CODE:
            raise UploadError(
                f"第 {lineno} 行：标记「{mark_text}」无效，"
                f"应为 有坡道 / 无坡道 / 看不清")
        pairs[image_no] = MARK_TO_CODE[mark_text]
    if not pairs:
        raise UploadError("CSV 中没有有效数据行")
    return pairs


# ---------------------------------------------------------------- 统计

def pair_counts(data_a, data_b):
    """只对双方都判读为有/无坡道的图配对，返回四格计数与分歧图号。"""
    n11 = n10 = n01 = n00 = 0
    disagreements = []
    for image_no in range(1, MAX_IMAGE_NO + 1):
        ca = unpack_code(data_a, image_no)
        cb = unpack_code(data_b, image_no)
        if ca not in VALID_CODES or cb not in VALID_CODES:
            continue
        if ca == 1 and cb == 1:
            n11 += 1
        elif ca == 2 and cb == 2:
            n00 += 1
        elif ca == 1:
            n10 += 1
            disagreements.append(image_no)
        else:
            n01 += 1
            disagreements.append(image_no)
    return (n11, n10, n01, n00), disagreements


def cohen_kappa(n11, n10, n01, n00):
    """返回 (kappa, None) 或 (None, 无法比较的原因)。"""
    total = n11 + n10 + n01 + n00
    if total == 0:
        return None, "没有双方都判读为「有坡道/无坡道」的有效配对"
    po = (n11 + n00) / total
    pe = ((n11 + n10) * (n11 + n01) + (n01 + n00) * (n10 + n00)) / (total * total)
    denom = 1.0 - pe
    if denom == 0:
        return None, "κ 分母为零（两人边际分布完全一致）"
    return (po - pe) / denom, None


def fetch_summary(con, annotator):
    return con.execute(
        "SELECT annotator, version, data, length(data) AS size, updated_at "
        "FROM summaries WHERE annotator = ?", (annotator,)).fetchone()


# ---------------------------------------------------------------- 路由

@app.route("/")
def index():
    annotators = get_db().execute(
        "SELECT s.annotator, s.version, s.updated_at, length(s.data) AS size, "
        "       (SELECT count(*) FROM annotations a "
        "         WHERE a.annotator = s.annotator) AS n "
        "FROM summaries s ORDER BY s.annotator").fetchall()
    return render_template("index.html", annotators=annotators,
                           max_image=MAX_IMAGE_NO)


@app.route("/upload", methods=["POST"])
def upload():
    annotator = (request.form.get("annotator") or "").strip()
    if not annotator:
        flash("请填写标注员姓名/工号", "error")
        return redirect(url_for("index"))
    if len(annotator) > 64:
        flash("标注员姓名/工号过长（不超过 64 字符）", "error")
        return redirect(url_for("index"))
    file = request.files.get("file")
    if file is None or not file.filename:
        flash("请选择要上传的 CSV 文件", "error")
        return redirect(url_for("index"))
    try:
        text = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("上传失败，已沿用原数据：文件不是有效的 UTF-8 编码", "error")
        return redirect(url_for("index"))
    try:
        pairs = parse_csv(text)
    except UploadError as exc:
        flash(f"上传失败，已沿用原数据：{exc}", "error")
        return redirect(url_for("index"))

    con = get_db()
    try:
        # 原始标注与汇总在同一事务中生效；只写本人数据
        con.execute("BEGIN IMMEDIATE")
        con.executemany(
            "INSERT INTO annotations (annotator, image_no, mark, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT (annotator, image_no) DO UPDATE SET "
            "mark = excluded.mark, updated_at = excluded.updated_at",
            [(annotator, img, code) for img, code in pairs.items()])
        rows = con.execute(
            "SELECT image_no, mark FROM annotations WHERE annotator = ?",
            (annotator,)).fetchall()
        blob = pack_summary((row["image_no"], row["mark"]) for row in rows)
        if len(blob) > SUMMARY_LIMIT:
            raise UploadError(
                f"汇总大小 {len(blob)} 字节超过 128 KiB 上限")
        con.execute(
            "INSERT INTO summaries (annotator, version, data, updated_at) "
            "VALUES (?, 1, ?, datetime('now')) "
            "ON CONFLICT (annotator) DO UPDATE SET "
            "version = version + 1, data = excluded.data, "
            "updated_at = excluded.updated_at",
            (annotator, blob))
        con.commit()
    except UploadError as exc:
        con.rollback()
        flash(f"上传失败，已沿用原数据：{exc}", "error")
        return redirect(url_for("index"))
    except sqlite3.Error as exc:
        con.rollback()
        flash(f"上传失败，已沿用原数据：数据库错误（{exc}）", "error")
        return redirect(url_for("index"))

    flash(f"上传成功：{annotator} 本次写入 {len(pairs)} 条，"
          f"累计 {len(rows)} 条，汇总 {len(blob)} 字节", "ok")
    return redirect(url_for("index"))


@app.route("/compare")
def compare():
    a = request.args.get("a", "")
    b = request.args.get("b", "")
    if not a or not b:
        flash("请选择两位标注员", "error")
        return redirect(url_for("index"))
    if a == b:
        flash("标注员 A 与 B 不能是同一人", "error")
        return redirect(url_for("index"))

    con = get_db()
    # 单读事务：计数与分歧清单取自同一已提交状态
    con.execute("BEGIN")
    try:
        ra = fetch_summary(con, a)
        rb = fetch_summary(con, b)
        if ra is None or rb is None:
            con.rollback()
            flash("所选标注员还没有汇总数据", "error")
            return redirect(url_for("index"))
        (n11, n10, n01, n00), disagreements = pair_counts(ra["data"], rb["data"])
        kappa, kappa_error = cohen_kappa(n11, n10, n01, n00)
        result = dict(a=a, b=b, va=ra["version"], vb=rb["version"],
                      size_a=ra["size"], size_b=rb["size"],
                      n11=n11, n10=n10, n01=n01, n00=n00,
                      total=n11 + n10 + n01 + n00,
                      kappa=kappa, kappa_error=kappa_error,
                      dis_count=len(disagreements))
        con.commit()
    except Exception:
        con.rollback()
        raise
    return render_template("compare.html", **result)


@app.route("/disagreements.csv")
def disagreements():
    a = request.args.get("a", "")
    b = request.args.get("b", "")
    va = request.args.get("va", type=int)
    vb = request.args.get("vb", type=int)
    if not a or not b or va is None or vb is None:
        return "参数不完整", 400

    con = get_db()
    con.execute("BEGIN")
    try:
        ra = fetch_summary(con, a)
        rb = fetch_summary(con, b)
        if ra is None or rb is None:
            con.rollback()
            return "所选标注员还没有汇总数据", 404
        if ra["version"] != va or rb["version"] != vb:
            con.rollback()
            return "数据已更新，本次核查结果已失效，请重新核查后再下载", 409
        _, images = pair_counts(ra["data"], rb["data"])
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["图号", f"{a}的判读", f"{b}的判读"])
        for image_no in images:
            writer.writerow([
                image_no,
                CODE_TO_MARK[unpack_code(ra["data"], image_no)],
                CODE_TO_MARK[unpack_code(rb["data"], image_no)],
            ])
        con.commit()
    except Exception:
        con.rollback()
        raise

    return Response(
        out.getvalue().encode("utf-8-sig"),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; "
                                        "filename=disagreements.csv"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
