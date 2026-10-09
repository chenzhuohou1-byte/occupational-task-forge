"""造题节点：N1 选材 / N2a 事实核查+证据链校验 / N3~N5 起草。

正解在模型之外，故与老的数学生题管线的根本区别是：数字必须走 N2a 证据链或 N2b 口径裁定，
判分依据只能来自 ①题面明写的约定规则 ②有可核查链接+原文引用的外部事实。
LLM 节点用 mock 可空跑；N2a 的证据链校验是纯代码、必须真跑。
"""
import json
import re
import urllib.error
import urllib.request

EVIDENCE_COLS = ("数值", "来源URL", "原文引用", "取数日期")

# 不少权威站点（实测 chinatax.gov.cn）对无 User-Agent 的裸请求直接 403，
# 不带 UA 会把真实存在的页面误判成「不可达」。统一发一个常见浏览器 UA。
_FETCH_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")


def n1_select(llm, occupation, n=10, cost=None):
    """N1 选材：职位 → n 个真实、需专业软件操作的工作面（agent 生成，人勾选）。"""
    msgs = [
        {"role": "system", "content": "你是评测数据集选材专家。"},
        {"role": "user", "content": "针对职业「%s」，列出 %d 个真实、具体、"
         "需要用专业软件操作完成的工作面，每行一个，只输出清单。" % (occupation, n)},
    ]
    res = llm.chat(msgs)
    if cost:
        cost.add("N1", res)
    faces = [re.sub(r"^[\s\-·.、0-9]+", "", ln).strip()
             for ln in res.text.splitlines() if ln.strip()]
    return True, {"occupation": occupation, "faces": faces, "raw": res.text}


def n2a_factcheck(llm, claim, cost=None, fetch=True):
    """N2a 事实核查：让 LLM 就断言产出证据链，再机械校验。"""
    msgs = [
        {"role": "system", "content": "你做事实核查，必须给出可核查证据链，不得编造。"},
        {"role": "user", "content": "就以下断言给出证据链，严格输出 JSON 数组，每项含字段"
         "%s：%s" % (list(EVIDENCE_COLS), claim)},
    ]
    res = llm.chat(msgs)
    if cost:
        cost.add("N2a", res)
    m = re.search(r"\[.*\]", res.text, re.S)
    if not m:
        return False, {"error": "证据链非合法 JSON 数组", "raw": res.text}
    try:
        rows = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False, {"error": "证据链解析失败", "raw": res.text}
    ok, checked = verify_evidence(rows, fetch=fetch)
    return ok, {"claim": claim, "evidence": checked, "通过": ok}


def verify_evidence(rows, fetch=True):
    """证据链机械校验（纯代码，必须真跑）：
      ① 四列齐全 ② 来源 URL 真可达 ③ 原文确含引用片段。
    这道拦的是联网新增的失败模式——编造看起来权威的引用（域名/标准号像真的，原文没这句）。
    """
    all_ok = True
    out = []
    for row in rows:
        rec = dict(row)
        problems = []
        for c in EVIDENCE_COLS:
            if not str(row.get(c, "")).strip():
                problems.append("缺列:" + c)
        url = str(row.get("来源URL", "")).strip()
        quote = str(row.get("原文引用", "")).strip()
        if url and fetch and not problems:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": _FETCH_UA})
                html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
                squeezed = re.sub(r"\s+", "", html)
                if quote and quote not in html and re.sub(r"\s+", "", quote) not in squeezed:
                    problems.append("原文未包含引用片段")
            except urllib.error.HTTPError as e:  # 区分 403 反爬 / 404 不存在 / 其他状态码
                problems.append("URL不可达:HTTP%d" % e.code)
            except Exception as e:  # noqa: BLE001 网络/解析异常统一记为不可达
                problems.append("URL不可达:" + type(e).__name__)
        rec["_problems"] = problems
        rec["_ok"] = not problems
        all_ok = all_ok and not problems
        out.append(rec)
    return all_ok, out


def n3_5_draft(llm, occupation, face, conventions=None, cost=None):
    """N3~N5 起草：给定工作面 + 已裁定口径，产出一份任务草案（题面/交付要求/正解要点/附件计划）。

    注意：这里只产出**草案 JSON**，真正落成可跑任务（task_card.json + main.py + assets）
    仍需按模板 tasks/工程造价/综合单价复核/ 定稿——正解数值只许用 conventions 或 N2a 证据链里的数字。
    """
    msgs = [
        {"role": "system", "content": "你把一个工作面草拟成 agent 评测任务，像真实派活、不写步骤。"},
        {"role": "user", "content": "职业:%s\n工作面:%s\n可用约定口径:%s\n"
         "输出 JSON，含字段：prompt、交付要求(对象)、正解要点(数组)、附件计划(数组)。"
         % (occupation, face, json.dumps(conventions or [], ensure_ascii=False))},
    ]
    res = llm.chat(msgs)
    if cost:
        cost.add("N3-5", res)
    m = re.search(r"\{.*\}", res.text, re.S)
    draft = {}
    if m:
        try:
            draft = json.loads(m.group(0))
        except json.JSONDecodeError:
            draft = {}
    return bool(draft), {"occupation": occupation, "face": face,
                         "draft": draft, "raw": res.text}
