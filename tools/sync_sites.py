"""
把 service/sites.py 里的机组清单同步进 Dify 工作流 DSL。

Dify 的下拉选项只能写死在 DSL 里，读不了接口，所以清单必然存两份。
手动维护两处迟早会漂移 —— 台账按 id 存，DSL 里少一项就永远录不进那个位置。
改完 sites.py 跑一下这个脚本，别手改 DSL。

    python tools/sync_sites.py            写回 dify/workflow.yml
    python tools/sync_sites.py --check    只检查是否一致（CI 用），不改文件
"""

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from service import sites   # noqa: E402

DSL = os.path.join(ROOT, "dify", "workflow.yml")


def build_options():
    return ["%s" % s["label"] for s in sites.SITES]


def render_block(indent="        "):
    """生成 start 节点里 site 变量的 options 列表。"""
    lines = ["%soptions:" % indent]
    for o in build_options():
        lines.append("%s- '%s'" % (indent, o.replace("'", "''")))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(DSL):
        print("找不到 %s" % DSL)
        return 1
    src = open(DSL, encoding="utf-8").read()

    # 只替换 site 变量那一段的 options，别碰别的节点
    pat = re.compile(
        r"(- variable: site\b.*?\n)(\s*)options:\n(?:\s*- .*\n)*",
        re.S,
    )
    m = pat.search(src)
    if not m:
        print("DSL 里没找到 site 变量的 options 段落。")
        print("如果 start 节点还没改成下拉，先在 Dify 里把 site 改成「下拉选项」再导出。")
        return 1

    new = m.group(1) + render_block(m.group(2)) + "\n"
    if src[m.start():m.end()] == new:
        print("已是最新，%d 个选项。" % len(sites.SITES))
        return 0

    if a.check:
        print("不一致：DSL 里的选项和 sites.py 对不上，跑 python tools/sync_sites.py 同步。")
        return 1

    open(DSL, "w", encoding="utf-8", newline="\n").write(
        src[:m.start()] + new + src[m.end():])
    print("已写回 %d 个选项到 %s" % (len(sites.SITES), os.path.relpath(DSL, ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
