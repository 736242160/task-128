#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hanzi_merge.py — 汉字部件归并工具（纯 Python 标准库，单文件）

功能：
  1. 读取部件定义（部件名 + 异体列表）、字结构定义（字 + 部件列表）、
     归并操作流（归并部件 -> 目标部件）。
  2. 归并后引用被归并部件的字级联（可传递）更新为目标部件。
  3. 错误报告：
     - 归并目标不存在            MERGE_TARGET_MISSING
     - 归并源部件不存在          MERGE_SOURCE_MISSING
     - 归并后部件重复（已等价）  DUPLICATE_MERGE
     - 归并形成环（甲->乙->甲）  MERGE_CYCLE（报告环上部件）
     - 字引用不存在的部件        UNDEFINED_COMPONENT_REF
     - 异体定义重复              DUPLICATE_VARIANT
     - 部件/字重复定义           DUPLICATE_COMPONENT / DUPLICATE_CHAR
  4. 归并后字结构校验（部件合法性）。
  5. 跨操作状态延续，归并历史可追溯（每个部件的归并链可查询）。

输入格式（纯文本，行导向，# 之后为注释，空行忽略）：
  部件 名称 异体1 异体2 ...     （或 component / comp / c）
  字   名称 部件1 部件2 ...     （或 char / z）
  归并 源部件 目标部件          （或 merge / m；支持 "源 -> 目标" 写法）

用法：
  python3 hanzi_merge.py 输入文件 [更多文件...]
  python3 hanzi_merge.py < 输入文件
  python3 hanzi_merge.py --demo          # 运行内置示例
  python3 hanzi_merge.py --json 输入文件 # 输出 JSON
退出码：0 = 无错误；1 = 存在错误。
"""

from __future__ import annotations

import argparse
import json
import sys

# ---------------- 错误码 ----------------
E_PARSE = "PARSE_ERROR"
E_DUP_COMPONENT = "DUPLICATE_COMPONENT"
E_DUP_CHAR = "DUPLICATE_CHAR"
E_DUP_VARIANT = "DUPLICATE_VARIANT"
E_TARGET_MISSING = "MERGE_TARGET_MISSING"
E_SOURCE_MISSING = "MERGE_SOURCE_MISSING"
E_DUP_MERGE = "DUPLICATE_MERGE"
E_CYCLE = "MERGE_CYCLE"
E_UNDEF_REF = "UNDEFINED_COMPONENT_REF"

COMPONENT_DIRECTIVES = {"部件", "component", "comp", "c"}
CHAR_DIRECTIVES = {"字", "char", "z"}
MERGE_DIRECTIVES = {"归并", "merge", "m"}
ARROWS = {"->", "→", "=>", "⇒"}


class HanziMerger:
    """部件归并状态机：顺序处理定义与归并操作，状态跨操作延续。"""

    def __init__(self):
        self.comp_variants = {}   # 部件名 -> [异体, ...]（定义顺序）
        self.chars = {}           # 字名 -> [部件名, ...]（原始定义）
        self.parent = {}          # 部件名 -> (归并目标, 操作序号)，构成归并链
        self.errors = []          # [{"code", "message"}, ...]
        self.history = []         # 归并操作历史（含成功与失败）
        self._variant_owner = {}  # 异体 -> 所属部件名
        self._op_seq = 0

    # ---------------- 基础工具 ----------------
    def _add_error(self, code, message):
        self.errors.append({"code": code, "message": message})

    def _chain(self, name):
        """从 name 沿归并链到根的节点序列（含 name 与根）。"""
        chain = [name]
        seen = {name}
        cur = name
        while cur in self.parent:
            nxt = self.parent[cur][0]
            if nxt in seen:  # 防御：环不会被应用，理论上不会触发
                break
            chain.append(nxt)
            seen.add(nxt)
            cur = nxt
        return chain

    def resolve(self, name):
        """求部件的最终规范名（级联解析，传递闭包）。"""
        return self._chain(name)[-1]

    def _exists(self, name):
        return self.resolve(name) in self.comp_variants

    # ---------------- 定义处理 ----------------
    def define_component(self, name, variants, lineno):
        if name in self.comp_variants:
            self._add_error(E_DUP_COMPONENT,
                            f"第{lineno}行：部件「{name}」重复定义，已忽略")
            return
        self.comp_variants[name] = []
        for v in variants:
            if v in self.comp_variants[name]:
                self._add_error(E_DUP_VARIANT,
                                f"第{lineno}行：部件「{name}」的异体「{v}」在同一定义中重复")
                continue
            owner = self._variant_owner.get(v)
            if owner is not None:
                self._add_error(E_DUP_VARIANT,
                                f"第{lineno}行：异体「{v}」重复定义"
                                f"（已属于部件「{owner}」，又用于部件「{name}」）")
            else:
                self._variant_owner[v] = name
            self.comp_variants[name].append(v)

    def define_char(self, name, comps, lineno):
        if not comps:
            self._add_error(E_PARSE, f"第{lineno}行：字「{name}」未给出任何部件")
            return
        if name in self.chars:
            self._add_error(E_DUP_CHAR, f"第{lineno}行：字「{name}」重复定义，已忽略")
            return
        self.chars[name] = list(comps)

    # ---------------- 归并操作 ----------------
    def merge(self, source, target, lineno):
        self._op_seq += 1
        seq = self._op_seq
        rec = {"seq": seq, "source": source, "target": target,
               "lineno": lineno, "status": "applied", "message": ""}
        self.history.append(rec)

        def fail(code, msg):
            rec["status"] = "error"
            rec["message"] = msg
            self._add_error(code, f"操作#{seq}（第{lineno}行）：{msg}"
                                  f"（归并 {source} → {target} 未执行）")

        if not self._exists(target):
            fail(E_TARGET_MISSING, f"归并目标「{target}」不存在")
            return
        if not self._exists(source):
            fail(E_SOURCE_MISSING, f"归并源部件「{source}」不存在")
            return

        rs, rt = self.resolve(source), self.resolve(target)
        if rs == rt:
            if source == target:
                fail(E_DUP_MERGE, f"部件「{source}」与自身本就等价，归并重复")
            elif source in self._chain(target):
                # target 的归并链经过 source，加边 source→target 将成环
                members = self._chain(target)
                members = members[:members.index(source) + 1]
                path = " → ".join([source] + members)
                fail(E_CYCLE, f"归并形成环：{path}（环上部件：{'、'.join(members)}）")
            else:
                fail(E_DUP_MERGE,
                     f"部件「{source}」与「{target}」已等价（同归并于「{rs}」），归并重复")
            return

        # 应用归并：整条 source 链的根挂到 target 链的根上（级联生效）
        self.parent[rs] = (rt, seq)
        rec["canonical"] = rt
        rec["message"] = f"「{rs}」归并入「{rt}」"

    # ---------------- 归并后字结构校验 ----------------
    def validate_chars(self):
        seen = set()
        for ch, comps in self.chars.items():
            for c in comps:
                if (ch, c) in seen:
                    continue
                seen.add((ch, c))
                if not self._exists(c):
                    self._add_error(E_UNDEF_REF,
                                    f"字「{ch}」引用了不存在的部件「{c}」")

    # ---------------- 行解析 ----------------
    def process_line(self, raw, lineno):
        line = raw.split("#", 1)[0].strip()
        if not line:
            return
        tokens = line.split()
        directive = tokens[0].lower()
        args = [t for t in tokens[1:] if t not in ARROWS]
        if directive in COMPONENT_DIRECTIVES:
            if not args:
                self._add_error(E_PARSE, f"第{lineno}行：部件定义缺少名称")
                return
            self.define_component(args[0], args[1:], lineno)
        elif directive in CHAR_DIRECTIVES:
            if not args:
                self._add_error(E_PARSE, f"第{lineno}行：字定义缺少名称")
                return
            self.define_char(args[0], args[1:], lineno)
        elif directive in MERGE_DIRECTIVES:
            if len(args) != 2:
                self._add_error(E_PARSE,
                                f"第{lineno}行：归并操作需要恰好两个部件（源 目标）")
                return
            self.merge(args[0], args[1], lineno)
        else:
            self._add_error(E_PARSE, f"第{lineno}行：无法识别的指令「{tokens[0]}」")

    def process_text(self, text):
        for i, raw in enumerate(text.splitlines(), 1):
            self.process_line(raw, i)

    # ---------------- 结果汇总 ----------------
    def component_table(self):
        """归并后的部件表：规范部件 -> {成员, 异体}。"""
        table = {}
        for name, variants in self.comp_variants.items():
            root = self.resolve(name)
            entry = table.setdefault(root, {"members": [], "variants": []})
            entry["members"].append(name)
            for v in variants:
                if v not in entry["variants"]:
                    entry["variants"].append(v)
        return table

    def merged_chars(self):
        """归并后的字结构：每个部件级联解析为最终规范部件。"""
        return {ch: [self.resolve(c) for c in comps]
                for ch, comps in self.chars.items()}

    def traces(self):
        """归并历史追溯：每个被归并部件的完整归并链（含操作序号）。"""
        result = {}
        for name in self.comp_variants:
            if name not in self.parent:
                continue
            steps = []
            cur = name
            seen = {name}
            while cur in self.parent:
                nxt, seq = self.parent[cur]
                steps.append({"from": cur, "to": nxt, "op": seq})
                if nxt in seen:
                    break
                seen.add(nxt)
                cur = nxt
            result[name] = steps
        return result

    def report(self):
        self.validate_chars()
        return {
            "component_table": self.component_table(),
            "merged_chars": self.merged_chars(),
            "history": self.history,
            "traces": self.traces(),
            "errors": self.errors,
        }


# ---------------- 输出 ----------------
def render_text(rep):
    out = []
    out.append("========== 归并后部件表 ==========")
    if rep["component_table"]:
        for root, e in rep["component_table"].items():
            members = "、".join(e["members"])
            variants = "、".join(e["variants"]) if e["variants"] else "（无）"
            merged = f"（归并成员：{members}）" if len(e["members"]) > 1 else ""
            out.append(f"{root} {merged} 异体：{variants}")
    else:
        out.append("（无部件定义）")

    out.append("")
    out.append("========== 归并后字结构 ==========")
    if rep["merged_chars"]:
        for ch, comps in rep["merged_chars"].items():
            raw = " ".join(comps)
            out.append(f"{ch} = {raw}")
    else:
        out.append("（无字结构定义）")

    out.append("")
    out.append("========== 归并历史（可追溯） ==========")
    if rep["history"]:
        for rec in rep["history"]:
            mark = "成功" if rec["status"] == "applied" else "失败"
            out.append(f"#{rec['seq']} 归并 {rec['source']} → {rec['target']} "
                       f"[{mark}] {rec['message']}")
    else:
        out.append("（无归并操作）")
    if rep["traces"]:
        out.append("---- 部件归并链 ----")
        for name, steps in rep["traces"].items():
            path = " → ".join([steps[0]["from"]] + [s["to"] for s in steps])
            ops = "、".join(f"#{s['op']}" for s in steps)
            out.append(f"{name}：{path}（经操作 {ops}）")

    out.append("")
    out.append("========== 错误清单 ==========")
    if rep["errors"]:
        for i, e in enumerate(rep["errors"], 1):
            out.append(f"[E{i:02d}][{e['code']}] {e['message']}")
        out.append(f"共 {len(rep['errors'])} 个错误")
    else:
        out.append("（无错误）")
    return "\n".join(out)


# ---------------- 内置示例 ----------------
DEMO_INPUT = """\
# ---- 部件定义 ----
部件 氵 氵 氺
部件 水 水
部件 工
部件 口
部件 木 木
部件 林 木        # 异体「木」已属于部件「木」→ 异体重复
# ---- 字结构定义 ----
字 江 氵 工
字 河 氵 可       # 「可」未定义 → 字引用不存在的部件
字 杏 木 口
# ---- 归并操作流 ----
归并 氵 水        # 成功：江、河 中的 氵 级联更新为 水
归并 水 氵        # 失败：与上一条构成环（氵 → 水 → 氵）
归并 木 林        # 成功
归并 口 林        # 成功
归并 木 口        # 失败：木、口均已归并于林，重复归并
归并 工 不存在    # 失败：归并目标不存在
归并 林 木        # 失败：林 → 木 → 林 成环
"""


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="汉字部件归并工具（纯标准库单文件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("files", nargs="*", help="输入文件（可多个，按顺序拼接；缺省读标准输入）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    ap.add_argument("--demo", action="store_true", help="运行内置示例")
    ap.add_argument("--encoding", default="utf-8", help="输入文件编码（默认 utf-8）")
    args = ap.parse_args(argv)

    merger = HanziMerger()
    if args.demo:
        merger.process_text(DEMO_INPUT)
    elif args.files:
        for path in args.files:
            try:
                with open(path, "r", encoding=args.encoding) as f:
                    merger.process_text(f.read())
            except OSError as exc:
                merger._add_error(E_PARSE, f"无法读取文件 {path}：{exc}")
    else:
        merger.process_text(sys.stdin.read())

    rep = merger.report()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render_text(rep))
    return 1 if rep["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
