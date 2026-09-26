#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hanzi_merge.py — 汉字部件归并工具（纯 Python 标准库，单文件）

功能：
  1. 读取部件定义（名称 + 异体列表）、字结构定义（名称 + 部件列表）、
     归并操作流（归并部件 -> 目标部件）。
  2. 归并后级联更新：所有引用被归并部件的字，自动解析到最终目标部件。
  3. 错误报告：
     - 归并目标 / 归并源不存在
     - 归并两部件已等价（重复归并）
     - 归并形成环（甲->乙、乙->甲），报告环上部件
     - 归并冲突（同一部件被归并到两个不同目标）
     - 字引用不存在的部件
     - 异体定义重复 / 异体与正名冲突
     - 归并后字结构校验（部件合法、归并后部件重复）
  4. 跨操作状态延续，归并历史逐步可追溯。
  5. 输出归并后字结构、部件表、归并历史与错误清单（文本或 JSON）。

输入格式（每行一条指令，# 开头为注释，支持中英文关键字）：
  部件 口 囗            # component <名称> <异体1> <异体2> ...
  字 休 亻 木           # char <名称> <部件1> <部件2> ...
  归并 囗 口            # merge <归并部件> <目标部件>（可用 -> / → / 到 分隔）

用法：
  python3 hanzi_merge.py 输入文件            # 文本报告输出到 stdout
  python3 hanzi_merge.py 输入文件 --json     # JSON 报告
  python3 hanzi_merge.py -                   # 从标准输入读取
  python3 hanzi_merge.py 输入文件 -o out.txt # 报告写入文件
退出码：0 = 无错误；1 = 存在错误；2 = 参数/IO 错误。
"""

import argparse
import json
import sys

COMPONENT_KEYS = {"component", "部件"}
CHAR_KEYS = {"char", "字"}
MERGE_KEYS = {"merge", "归并"}
SEPARATORS = {"->", "→", "到", "=", ":"}


class Merger:
    """维护部件表、异体表、归并边（每个部件至多一条出边）与字结构。"""

    def __init__(self):
        self.components = {}      # 正名 -> {"variants": [...], "line": int}
        self.variant_owner = {}   # 异体名 -> 所属正名
        self.chars = {}           # 字 -> [原始部件名, ...]
        self.char_lines = {}      # 字 -> 定义行号
        self.merge_to = {}        # 正名 -> 正名（归并边，有向）
        self.errors = []          # [{"line", "kind", "message"}]
        self.history = []         # 归并操作历史（含被否决的操作）

    # ---------- 基础查询 ----------

    def canonical_of(self, token):
        """名字（正名或异体）-> 部件正名；不存在返回 None。"""
        if token in self.components:
            return token
        return self.variant_owner.get(token)

    def chain(self, canonical):
        """从正名出发沿归并边的完整路径（含起点与终点）。"""
        path = [canonical]
        node = canonical
        while node in self.merge_to:
            node = self.merge_to[node]
            if node in path:  # 防御：正常流程不会成环
                path.append(node)
                break
            path.append(node)
        return path

    def resolve(self, canonical):
        """解析到最终有效部件（归并链末端）。"""
        return self.chain(canonical)[-1]

    def error(self, line, kind, message):
        self.errors.append({"line": line, "kind": kind, "message": message})

    # ---------- 定义 ----------

    def add_component(self, line, name, variants):
        if name in self.components:
            self.error(line, "duplicate-component",
                       "部件「%s」重复定义（首次定义于第 %d 行）"
                       % (name, self.components[name]["line"]))
            return
        if name in self.variant_owner:
            self.error(line, "name-collision",
                       "部件名「%s」与部件「%s」的异体重名"
                       % (name, self.variant_owner[name]))
            return
        self.components[name] = {"variants": [], "line": line}
        seen = set()
        for variant in variants:
            if variant == name:
                self.error(line, "duplicate-variant",
                           "部件「%s」的异体「%s」与正名相同" % (name, variant))
                continue
            if variant in seen:
                self.error(line, "duplicate-variant",
                           "部件「%s」的异体「%s」在同一部件内重复定义" % (name, variant))
                continue
            if variant in self.components:
                self.error(line, "name-collision",
                           "异体「%s」与部件正名冲突" % variant)
                continue
            if variant in self.variant_owner:
                self.error(line, "duplicate-variant",
                           "异体「%s」重复定义：已属于部件「%s」，不能再属于「%s」"
                           % (variant, self.variant_owner[variant], name))
                continue
            seen.add(variant)
            self.variant_owner[variant] = name
            self.components[name]["variants"].append(variant)

    def add_char(self, line, name, parts):
        if name in self.chars:
            self.error(line, "duplicate-char",
                       "字「%s」重复定义（首次定义于第 %d 行）"
                       % (name, self.char_lines[name]))
            return
        if not parts:
            self.error(line, "empty-char", "字「%s」没有部件" % name)
            return
        self.chars[name] = list(parts)
        self.char_lines[name] = line

    # ---------- 归并 ----------

    def merge(self, line, src_token, dst_token):
        record = {"line": line, "source": src_token, "target": dst_token,
                  "status": "", "detail": ""}
        self.history.append(record)

        src = self.canonical_of(src_token)
        dst = self.canonical_of(dst_token)
        if src is None:
            record["status"] = "rejected"
            record["detail"] = "归并源「%s」不存在" % src_token
            self.error(line, "merge-source-missing",
                       "归并源部件「%s」不存在" % src_token)
            return
        if dst is None:
            record["status"] = "rejected"
            record["detail"] = "归并目标「%s」不存在" % dst_token
            self.error(line, "merge-target-missing",
                       "归并目标部件「%s」不存在" % dst_token)
            return
        if src == dst:
            record["status"] = "rejected"
            record["detail"] = "「%s」与自身等价，无需归并" % src
            self.error(line, "merge-equivalent",
                       "归并源与目标相同（「%s」），两部件已等价" % src)
            return

        # 环检测：从目标出发的归并链若经过源，则 src -> dst 会成环
        path = self.chain(dst)
        if src in path:
            cycle = [src] + path[:path.index(src) + 1]
            record["status"] = "rejected"
            record["detail"] = "形成归并环：%s" % " → ".join(cycle)
            self.error(line, "merge-cycle",
                       "归并形成环，环上部件：%s" % " → ".join(cycle))
            return

        # 等价检测：源与目标已解析到同一部件
        if self.resolve(src) == self.resolve(dst):
            record["status"] = "rejected"
            record["detail"] = "「%s」与「%s」已等价（同为「%s」），重复归并" % (
                src, dst, self.resolve(src))
            self.error(line, "merge-equivalent",
                       "部件「%s」与「%s」已等价（均解析为「%s」），归并后部件重复"
                       % (src, dst, self.resolve(src)))
            return

        # 冲突检测：源已有指向其他目标的归并边
        if src in self.merge_to:
            record["status"] = "rejected"
            record["detail"] = "「%s」已归并到「%s」，与目标「%s」冲突" % (
                src, self.merge_to[src], dst)
            self.error(line, "merge-conflict",
                       "部件「%s」已归并到「%s」，不能再归并到「%s」"
                       % (src, self.merge_to[src], dst))
            return

        self.merge_to[src] = dst
        record["status"] = "applied"
        record["detail"] = "已应用，解析链：%s" % " → ".join(self.chain(src))

    # ---------- 终态校验与输出 ----------

    def finalize(self):
        """校验全部字结构，返回归并后的字 -> 部件列表。"""
        structures = {}
        for char, parts in self.chars.items():
            line = self.char_lines[char]
            resolved = []
            for token in parts:
                canon = self.canonical_of(token)
                if canon is None:
                    self.error(line, "unknown-component",
                               "字「%s」引用了不存在的部件「%s」" % (char, token))
                    continue
                final = self.resolve(canon)
                if final not in self.components:
                    self.error(line, "invalid-structure",
                               "字「%s」的部件「%s」归并后解析到未定义部件「%s」"
                               % (char, token, final))
                    continue
                resolved.append(final)
            seen, dups = set(), []
            for part in resolved:
                if part in seen and part not in dups:
                    dups.append(part)
                seen.add(part)
            if dups:
                self.error(line, "duplicate-component-in-char",
                           "字「%s」归并后部件重复：%s（两部件已等价）"
                           % (char, "、".join(dups)))
            structures[char] = resolved
        return structures

    def component_table(self):
        """部件表：有效部件（含异体）与已归并部件（指向最终目标）。"""
        active, merged = {}, {}
        for name, info in self.components.items():
            if name in self.merge_to:
                merged[name] = self.resolve(name)
            else:
                active[name] = list(info["variants"])
        return {"active": active, "merged": merged}


# ---------- 解析 ----------

def parse(text, merger):
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = [t for t in line.split() if t not in SEPARATORS]
        if not tokens:
            continue
        key, args = tokens[0], tokens[1:]
        if key in COMPONENT_KEYS:
            if not args:
                merger.error(lineno, "parse", "部件定义缺少名称")
            else:
                merger.add_component(lineno, args[0], args[1:])
        elif key in CHAR_KEYS:
            if not args:
                merger.error(lineno, "parse", "字定义缺少名称")
            else:
                merger.add_char(lineno, args[0], args[1:])
        elif key in MERGE_KEYS:
            if len(args) != 2:
                merger.error(lineno, "parse",
                             "归并操作需要两个部件名，实际得到 %d 个" % len(args))
            else:
                merger.merge(lineno, args[0], args[1])
        else:
            merger.error(lineno, "parse",
                         "无法识别的指令「%s」（支持：部件/component、字/char、归并/merge）" % key)


# ---------- 报告 ----------

def render_text(merger, structures):
    out = []
    out.append("=== 归并后字结构 ===")
    if structures:
        for char, parts in structures.items():
            out.append("%s = %s" % (char, " ".join(parts)))
    else:
        out.append("（无字定义）")

    table = merger.component_table()
    out.append("")
    out.append("=== 部件表 ===")
    for name, variants in table["active"].items():
        suffix = "（异体：%s）" % " ".join(variants) if variants else ""
        out.append("%s%s" % (name, suffix))
    for name, final in table["merged"].items():
        out.append("%s → %s（已归并）" % (name, final))

    out.append("")
    out.append("=== 归并历史 ===")
    if merger.history:
        status_text = {"applied": "已应用", "rejected": "已拒绝"}
        for index, rec in enumerate(merger.history, 1):
            out.append("#%d [第 %d 行] 归并 %s → %s：%s（%s）" % (
                index, rec["line"], rec["source"], rec["target"],
                status_text.get(rec["status"], rec["status"]), rec["detail"]))
    else:
        out.append("（无归并操作）")

    out.append("")
    out.append("=== 错误清单 ===")
    if merger.errors:
        for err in merger.errors:
            out.append("[第 %d 行] %s: %s" % (err["line"], err["kind"], err["message"]))
    else:
        out.append("（无错误）")
    return "\n".join(out)


def render_json(merger, structures):
    return json.dumps({
        "structures": structures,
        "components": merger.component_table(),
        "history": merger.history,
        "errors": merger.errors,
    }, ensure_ascii=False, indent=2)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="汉字部件归并工具：级联更新引用，输出归并结果与错误清单。")
    parser.add_argument("input", help="输入文件路径，或 - 表示标准输入")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出报告")
    parser.add_argument("-o", "--output", help="报告输出文件（默认输出到标准输出）")
    args = parser.parse_args(argv)

    try:
        if args.input == "-":
            text = sys.stdin.read()
        else:
            with open(args.input, "r", encoding="utf-8") as fh:
                text = fh.read()
    except OSError as exc:
        print("无法读取输入：%s" % exc, file=sys.stderr)
        return 2

    merger = Merger()
    parse(text, merger)
    structures = merger.finalize()
    report = render_json(merger, structures) if args.json \
        else render_text(merger, structures)

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(report + "\n")
        except OSError as exc:
            print("无法写入输出：%s" % exc, file=sys.stderr)
            return 2
    else:
        print(report)
    return 1 if merger.errors else 0


if __name__ == "__main__":
    sys.exit(main())
