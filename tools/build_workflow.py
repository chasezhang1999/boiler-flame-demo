"""
在现有工作流前面插入「是不是炉膛火焰照片」的校验分支，生成新的 DSL。

为什么要这一步：开始节点只校验了文件类型，用户传张风景照或截图照样会走完
CV 和判级，最后出一份一本正经的结焦报告 —— 演示时被人随手传张无关图就穿帮了。

从线上导出的 graph 改，不是从仓库里那份改 —— 仓库那份是初版，界面上改过的
提示词和代码不在里面。

    python tools/build_workflow.py live_graph.json -o dify/workflow.yml
"""

import argparse
import copy
import json
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHECK_PROMPT = """你要判断一张照片是不是**电站锅炉炉膛内部**的火焰照片
（通过看火孔或炉膛监视摄像头拍摄的那种）。

只输出一个词，不要解释，不要标点：

- 是炉膛内部火焰照片 → FLAME
- 其他任何情况 → OTHER

注意这些都算 OTHER：风景、人物、文档、聊天截图、设备外观照、
仪表盘照片，以及蜡烛、篝火、烧烤、打火机等非炉膛的火焰。
看不清或过暗到无法判断，也输出 OTHER。"""

REJECT_CODE = '''def main() -> dict:
    md = "\\n".join([
        "# ⚠️ 这不是炉膛火焰照片",
        "",
        "上传的图片没有被识别为电站锅炉炉膛内部的火焰画面，已停止分析。",
        "",
        "请通过看火孔或炉膛监视摄像头重新拍摄，并确认：",
        "",
        "- 画面主体是炉膛内的火焰，不是设备外观或仪表",
        "- 曝光正常，不要过暗或过曝到看不出火焰形态",
        "- 尽量拍全火焰，不要只拍到一角",
    ])
    return {"summary": md, "report_url": "", "risk_level": "未分析"}
'''


def node(nid, data, x, y, w=244, h=90):
    return {
        "data": data, "height": h, "id": nid,
        "position": {"x": x, "y": y}, "positionAbsolute": {"x": x, "y": y},
        "selected": False, "sourcePosition": "right", "targetPosition": "left",
        "type": "custom", "width": w,
    }


def edge(src, dst, stype, dtype, handle="source"):
    return {
        "data": {"isInIteration": False, "isInLoop": False,
                 "sourceType": stype, "targetType": dtype},
        "id": "%s-%s-%s" % (src, handle, dst),
        "source": src, "sourceHandle": handle,
        "target": dst, "targetHandle": "target",
        "type": "custom", "zIndex": 0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("graph", help="从 Dify 导出的 live graph JSON")
    ap.add_argument("-o", "--out", default=os.path.join(ROOT, "dify", "workflow.yml"))
    a = ap.parse_args()

    g = json.load(open(a.graph, encoding="utf-8"))
    nodes = {n["id"]: n for n in g["nodes"]}
    if "check_node" in nodes:
        print("已经有 check_node 了，不重复插入。")
        return 0

    llm = nodes["llm_node"]
    model_cfg = copy.deepcopy(llm["data"]["model"])
    model_cfg["completion_params"] = {"temperature": 0}   # 判别题不需要发挥

    # 1) 图像校验节点：跟判级节点用同一个模型，视觉同样指向 start_node/photo
    g["nodes"].append(node("check_node", {
        "desc": "先判断是不是炉膛火焰照片，挡掉无关图片",
        "selected": False, "title": "图像校验", "type": "llm",
        "model": model_cfg,
        "prompt_template": [{"role": "system", "text": CHECK_PROMPT, "id": "chk-sys"}],
        "context": {"enabled": False, "variable_selector": []},
        "vision": {"enabled": True, "configs": {
            "variable_selector": ["start_node", "photo"], "detail": "low"}},
    }, 330, 60, h=98))

    # 2) 分支：输出里含 FLAME 才继续。要求模型只吐一个词，
    #    所以用 contains 足够；「不是火焰」这类回答里不含 FLAME，不会误判。
    g["nodes"].append(node("gate_node", {
        "desc": "", "selected": False, "title": "是火焰照片？", "type": "if-else",
        "cases": [{
            "case_id": "true", "id": "true", "logical_operator": "and",
            "conditions": [{
                "id": "cond-flame", "comparison_operator": "contains",
                "value": "FLAME", "varType": "string",
                "variable_selector": ["check_node", "text"],
            }],
        }],
    }, 640, 60, h=126))

    # 3) 否分支：给前端一段说明，不落台账（根本没走到 /report）
    g["nodes"].append(node("reject_node", {
        "desc": "输出重传提示", "selected": False, "title": "拒绝并说明",
        "type": "code", "code_language": "python3", "code": REJECT_CODE,
        "variables": [],
        "outputs": {
            "summary": {"type": "string", "children": None},
            "report_url": {"type": "string", "children": None},
            "risk_level": {"type": "string", "children": None},
        },
        "dependencies": [],
    }, 950, 180, h=54))

    g["nodes"].append(node("reject_end", {
        "desc": "", "selected": False, "title": "结束（未分析）", "type": "end",
        "outputs": [
            {"variable": "summary", "value_type": "string",
             "value_selector": ["reject_node", "summary"]},
            {"variable": "report_url", "value_type": "string",
             "value_selector": ["reject_node", "report_url"]},
            {"variable": "risk_level", "value_type": "string",
             "value_selector": ["reject_node", "risk_level"]},
        ],
    }, 1260, 180, h=142))

    # 4) 重接线：原来的 开始→CV分析 改成 开始→校验→分支→CV分析
    g["edges"] = [e for e in g["edges"]
                  if not (e["source"] == "start_node" and e["target"] == "http_node")]
    g["edges"] += [
        edge("start_node", "check_node", "start", "llm"),
        edge("check_node", "gate_node", "llm", "if-else"),
        edge("gate_node", "http_node", "if-else", "http-request", handle="true"),
        edge("gate_node", "reject_node", "if-else", "code", handle="false"),
        edge("reject_node", "reject_end", "code", "end"),
    ]

    # 原有节点整体右移，给新节点腾地方
    for nid in ("http_node", "parse_node", "llm_node", "report_node",
                "finish_node", "end_node"):
        if nid in nodes:
            nodes[nid]["position"]["x"] += 620
            nodes[nid]["positionAbsolute"]["x"] += 620

    dsl = {
        "app": {
            "description": "拍一张炉膛火焰照片，输出火焰轮廓图、相对亮温热力图和结焦风险评估报告。",
            "icon": "🔥", "icon_background": "#FFEAD5", "mode": "workflow",
            "name": "锅炉火焰结焦风险分析", "use_icon_as_answer_icon": False,
        },
        "dependencies": [], "kind": "app", "version": "0.7.0",
        "workflow": {
            "conversation_variables": [], "environment_variables": [],
            "features": {
                "file_upload": {"enabled": False},
                "opening_statement": "", "retriever_resource": {"enabled": False},
                "sensitive_word_avoidance": {"enabled": False},
                "speech_to_text": {"enabled": False}, "suggested_questions": [],
                "suggested_questions_after_answer": {"enabled": False},
                "text_to_speech": {"enabled": False},
            },
            "graph": g,
        },
    }
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(dsl, f, allow_unicode=True, sort_keys=False, width=100)
    print("已写出 %s：%d 个节点，%d 条边"
          % (os.path.relpath(a.out, ROOT), len(g["nodes"]), len(g["edges"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
