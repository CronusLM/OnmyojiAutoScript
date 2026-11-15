"""
OCR 模糊匹配工具模块（纯函数，不依赖 device）。

背景：阴阳师客户端对账号/服务器/角色的 OCR 识别结果不稳定——
  - 手机号中的 * 会被识别成 ★ ○ x B 等各种字符（如 150****4444 → 150★***4444）
  - 邮箱本地名会被识别出形近字（如 abc0123@163.com → a6co123 / abc023）
  - 服务器/角色名多一个少一个字、多出等级数字前缀（如 60 第五雨薇）

统一容错策略（三层兜底，命中即返回）：
  L1 digit_context_match  —— 掩码结构比较（针对 188****7062 类手机号）
  L2 email_local_match    —— 邮箱本地名比较
  L3 双阈值               —— 字符集合相似度 OR 编辑相似度（阈值参数化）

本模块只做文本比对，禁止 import device / BaseTask 等运行时依赖。
"""

import unicodedata

try:
    from module.logger import logger
except ImportError:  # 允许脱离仓库环境直接运行 __main__ 自检
    import logging

    logger = logging.getLogger("fuzzy_match")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

__all__ = [
    "normalize_text", "char_set_similarity", "edit_distance", "edit_similarity",
    "wildcard_edit_similarity", "parse_masked_account", "match_account",
    "match_name", "find_best_match",
]

# ---------- 字符混淆映射（OCR 常见形近字 → 数字） ----------
# 仅用于掩码/数字上下文，不用于普通文本
_DIGIT_CONFUSION = {
    "o": "0", "ò": "0", "ó": "0", "ō": "0", "õ": "0", "ô": "0",
    "l": "1", "i": "1", "|": "1", "!": "1",
    "b": "8",
    "s": "5",
    "z": "2",
    "q": "9", "g": "9",
    "d": "0",
}

# 账号中的星号类字形（掩码符号），统一归一为 '*'
_STAR_GLYPHS = "★✱＊✲✳∗☆◦○◎●〇×※﹡"


def normalize_text(text: str, kind: str = "account") -> str:
    """
    文本归一化。
    @param kind: 'account' | 'svr' | 'character'
        account   —— NFKC、小写、星号类字形统一为 '*'、去全部空白
        svr       —— NFKC、去空白
        character —— NFKC、去空白、剥离行首等级数字与装饰括号
    """
    if not text:
        return ""
    # NFKC：全角转半角等
    s = unicodedata.normalize("NFKC", text)
    # 去所有空白
    s = "".join(s.split())
    if kind == "account":
        s = s.lower()
        # 星号类字形统一为 '*'
        s = "".join("*" if ch in _STAR_GLYPHS else ch for ch in s)
    elif kind == "character":
        # 剥离行首等级数字与装饰括号（沿用旧版 lstrip 字符集，追加空格已由上面处理）
        s = s.lstrip("1234567890([<>])【】（）《》")
    return s


def char_set_similarity(a: str, b: str) -> float:
    """字符集合相似度 = |交集| / max(|集合|)；任一方为空返回 0。"""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / max(len(sa), len(sb))


def edit_distance(a: str, b: str) -> int:
    """标准编辑距离（插入/删除/替换，代价均为 1）。"""
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def edit_similarity(a: str, b: str) -> float:
    """编辑相似度 = 1 - dist/max(len)；任一方为空返回 0。"""
    if not a or not b:
        return 0.0
    return 1.0 - edit_distance(a, b) / max(len(a), len(b))


def wildcard_edit_similarity(a: str, b: str, wildcard: str = "*") -> float:
    """
    通配编辑相似度：'*' 与任意字符对齐时代价为 0。
    用于两侧都带掩码星号的账号比较（星号位置可能漂移）。
    """
    if not a or not b:
        return 0.0
    la, lb = len(a), len(b)
    prev = [0.0] * (lb + 1)
    for j in range(1, lb + 1):
        prev[j] = prev[j - 1] + (0.0 if b[j - 1] == wildcard else 1.0)
    for i in range(1, la + 1):
        cur = [0.0] * (lb + 1)
        cur[0] = prev[0] + (0.0 if a[i - 1] == wildcard else 1.0)
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cb = b[j - 1]
            if ca == cb or ca == wildcard or cb == wildcard:
                cost = 0.0
            else:
                cost = 1.0
            cur[j] = min(prev[j] + 1.0, cur[j - 1] + 1.0, prev[j - 1] + cost)
        prev = cur
    dist = prev[lb]
    return 1.0 - dist / max(la, lb)


def _to_digit_string(s: str) -> str:
    """混淆字母映射为数字，其余非数字字符丢弃，返回纯数字串。"""
    return "".join(_DIGIT_CONFUSION.get(ch, ch) for ch in s if ch.isdigit() or ch in _DIGIT_CONFUSION)


def parse_masked_account(text: str):
    """
    解析掩码账号，如 '188****7062' → ('188', 4, '7062')（数字部分已做混淆映射）。
    无 '*' 时返回 (None, 0, 纯数字串)，便于调用方区分；空串返回 None。
    """
    s = normalize_text(text, kind="account")
    if not s:
        return None
    star_count = s.count("*")
    digits = _to_digit_string(s)
    if star_count == 0:
        return (None, 0, digits)
    head, _, tail = digits.partition("*") if "*" in digits else (digits, "", "")
    # 注意：'*' 已被 _to_digit_string 丢弃，partition 不适用，改用按 '*' 分割原串再映射
    parts = s.split("*")
    head = _to_digit_string(parts[0])
    tail = _to_digit_string(parts[-1]) if len(parts) > 1 else ""
    return (head, star_count, tail)


def _tail_match(tail_a: str, tail_b: str) -> bool:
    """尾部匹配：允许一方是另一方的后缀（OCR 可能多识别/漏识别一个前导 0）。"""
    if not tail_a or not tail_b:
        return False
    return tail_a.endswith(tail_b) or tail_b.endswith(tail_a)


def _head_match(head_a: str, head_b: str) -> bool:
    """头部匹配：允许一方是另一方的前缀。"""
    if not head_a or not head_b:
        return False
    return head_a.startswith(head_b) or head_b.startswith(head_a)


def _structural_pair_match(a_raw: str, b_raw: str) -> float:
    """
    两个账号文本的结构化比较（L1 核心），返回得分（0=不匹配，1=结构命中）。
    覆盖三种形态：
      ① 双方都带掩码：头(前缀相等) + 星号数差≤1 + 尾(后缀相等，容差±1位)
      ② 目标带掩码、OCR 为纯数字串（≥7位）：OCR 数字串以头开头且以尾结尾
      ③ 纯数字别名：OCR 数字串以别名结尾或与别名相等
    """
    a = parse_masked_account(a_raw)
    b = parse_masked_account(b_raw)
    if a is None or b is None:
        return 0.0
    head_a, star_a, tail_a = a
    head_b, star_b, tail_b = b

    if star_a > 0 and star_b > 0:
        # ① 双方都带掩码
        if abs(star_a - star_b) > 1:
            return 0.0
        if not _head_match(head_a, head_b):
            return 0.0
        if _tail_match(tail_a, tail_b):
            return 1.0
        return 0.0

    if star_a > 0 and star_b == 0:
        # ② 目标带掩码、OCR 为纯数字（需 ≥ 头+尾长度且至少 7 位，防误命中）
        if len(tail_b) >= (len(head_a) + len(tail_a)) and len(tail_b) >= 7:
            if tail_b.startswith(head_a) and tail_b.endswith(tail_a):
                return 1.0
        return 0.0

    if star_a == 0 and star_b > 0:
        # 镜像情形（目标无掩码、OCR 带掩码）
        if len(tail_a) >= (len(head_b) + len(tail_b)) and len(tail_a) >= 7:
            if tail_a.startswith(head_b) and tail_a.endswith(tail_b):
                return 1.0
        return 0.0

    # 双方都无掩码：退化为 L3 处理（此处不重复判）
    return 0.0


def email_local_match(target: str, ocr_text: str,
                      set_threshold: float = 0.60, edit_threshold: float = 0.70) -> float:
    """
    L2 邮箱本地名比较：取 '@' 前的本地名做双阈值比较，返回得分（0=不匹配）。
    一方有 '@' 另一方没有时，用带 '@' 一方的本地名与另一方整体比较。
    """
    t = normalize_text(target, kind="account")
    o = normalize_text(ocr_text, kind="account")
    if not t or not o:
        return 0.0
    t_local = t.split("@")[0] if "@" in t else t
    o_local = o.split("@")[0] if "@" in o else o
    if not t_local or not o_local:
        return 0.0
    cs = char_set_similarity(t_local, o_local)
    es = edit_similarity(t_local, o_local)
    if cs >= set_threshold or es >= edit_threshold:
        return max(cs, es)
    return 0.0


def match_account(target: str, ocr_text: str,
                  set_threshold: float = 0.60, edit_threshold: float = 0.70,
                  aliases=()) -> tuple:
    """
    账号匹配主入口（L1 → L2 → L3 顺序兜底）。
    @param target: 配置的目标账号，如 '188****7062' 或 'abc0123@163.com'
    @param ocr_text: OCR 识别出的账号文本
    @param aliases: account_alias 中 '#' 分隔的别名列表（已知误识别形态）
    @return: (是否命中, 相似度得分)
    """
    t = normalize_text(target, kind="account")
    o = normalize_text(ocr_text, kind="account")
    if not t or not o:
        return (False, 0.0)

    best = 0.0

    # L1 结构化比较（目标 vs OCR）
    score = _structural_pair_match(t, o)
    if score > 0:
        return (True, score)

    # L1 结构化比较（别名 vs OCR）—— 别名是目标账号的已知误识别形态
    for alias in aliases:
        alias = normalize_text(alias, kind="account")
        if not alias:
            continue
        score = _structural_pair_match(alias, o)
        if score > 0:
            return (True, score)
        # 纯数字别名：OCR 数字串以别名结尾或与别名相等
        o_digits = parse_masked_account(o)[2] if parse_masked_account(o) else ""
        if alias.isdigit() and o_digits:
            if o_digits == alias or o_digits.endswith(alias):
                return (True, 1.0)

    # L2 邮箱本地名比较
    score = email_local_match(t, o, set_threshold, edit_threshold)
    if score > 0:
        return (True, score)
    best = max(best, 0.0)

    # L3 双阈值兜底：字符集合相似度 OR 编辑相似度
    # 双方都带掩码时用通配编辑相似度，否则用普通编辑相似度
    cs = char_set_similarity(t, o)
    if "*" in t and "*" in o:
        es = wildcard_edit_similarity(t, o)
    else:
        es = edit_similarity(t, o)
    if cs >= set_threshold or es >= edit_threshold:
        return (True, max(cs, es))
    return (False, max(cs, es))


# svr/character 的默认双阈值
_NAME_THRESHOLDS = {
    "svr": (0.60, 0.70),
    "character": (0.70, 0.75),
}


def match_name(target: str, ocr_text: str, kind: str = "svr",
               set_threshold: float = None, edit_threshold: float = None) -> tuple:
    """
    服务器/角色名匹配：双阈值 OR。
    @param kind: 'svr'（默认 0.60/0.70）| 'character'（默认 0.70/0.75）
    @return: (是否命中, 相似度得分)
    长度 <2 的文本只认全等（防单字符噪声误命中）。
    """
    if set_threshold is None or edit_threshold is None:
        d_set, d_edit = _NAME_THRESHOLDS.get(kind, (0.60, 0.70))
        set_threshold = set_threshold if set_threshold is not None else d_set
        edit_threshold = edit_threshold if edit_threshold is not None else d_edit

    t = normalize_text(target, kind=kind)
    o = normalize_text(ocr_text, kind=kind)
    if not t or not o:
        return (False, 0.0)
    if len(t) < 2 or len(o) < 2:
        hit = (t == o)
        return (hit, 1.0 if hit else 0.0)
    cs = char_set_similarity(t, o)
    es = edit_similarity(t, o)
    if cs >= set_threshold or es >= edit_threshold:
        return (True, max(cs, es))
    return (False, max(cs, es))


def find_best_match(target: str, candidates, kind: str = "svr",
                    set_threshold: float = None, edit_threshold: float = None,
                    aliases=()) -> tuple:
    """
    在候选列表中找与 target 最相似的项（取最高分，绝非首个命中即返回）。
    @param candidates: OCR 文本列表
    @param kind: 'svr' | 'character' | 'account'
    @return: (最佳候选下标, 得分)；全部低于阈值时返回 (-1, 最高分)
    长度归一化后 <2 的候选视为噪声跳过。
    """
    best_index, best_score = -1, 0.0
    for index, cand in enumerate(candidates):
        cand_norm = normalize_text(cand, kind="account" if kind == "account" else kind)
        if len(cand_norm) < 2:
            continue  # 噪声
        if kind == "account":
            hit, score = match_account(target, cand, set_threshold, edit_threshold, aliases)
        else:
            hit, score = match_name(target, cand, kind, set_threshold, edit_threshold)
        if score > best_score:
            best_score = score
            best_index = index
    if best_index < 0:
        return (-1, best_score)
    # 最高分候选仍需过阈值才算命中
    if best_score < min(set_threshold or _NAME_THRESHOLDS.get(kind, (0.60, 0.70))[0],
                        edit_threshold or _NAME_THRESHOLDS.get(kind, (0.60, 0.70))[1]):
        return (-1, best_score)
    return (best_index, best_score)


# ---------------- __main__ 自检用例（全部必须 PASS） ----------------
if __name__ == "__main__":
    failures = []

    def check(desc, cond):
        logger.info(f"[{'PASS' if cond else 'FAIL'}] {desc}")
        if not cond:
            failures.append(desc)

    # --- 账号：邮箱误识别 ---
    check("abc0123@163.com ↔ a6co123 命中", match_account("abc0123@163.com", "a6co123")[0])
    check("abc0123@163.com ↔ abc023 命中", match_account("abc0123@163.com", "abc023")[0])
    check("abc0123@163.com ↔ zzz999 不命中", not match_account("abc0123@163.com", "zzz999")[0])

    # --- 账号：掩码手机号 ---
    check("188****7062 ↔ 188★**o7062 命中", match_account("188****7062", "188★**o7062")[0])
    check("188****7062 ↔ 188oOO7062 命中", match_account("188****7062", "188oOO7062")[0])
    check("188****7062 ↔ 1BB****BBBB 无别名不命中",
          not match_account("188****7062", "1BB****BBBB")[0])
    check("188****7062 ↔ 1BB****BBBB 有别名命中",
          match_account("188****7062", "1BB****BBBB", aliases=("1BB****BBBB",))[0])
    check("别名 '7062' ↔ 188****7062 命中（纯数字别名吸收尾部）",
          match_account("188****7062", "188****7062", aliases=("7062",))[0])
    check("150****4444 ↔ 150★***4444 命中（README 示例）",
          match_account("150****4444", "150★***4444")[0])
    check("188****7062 ↔ 188****9999 不命中", not match_account("188****7062", "188****9999")[0])

    # --- 服务器名 ---
    hit, score = match_name("狐梦桃乡", "愿保乡", kind="svr")
    check(f"狐梦桃乡 ↔ 愿保乡 不命中 (score={score:.2f})", not hit)
    check("愿保乡 ↔ 愿保乡 命中", match_name("愿保乡", "愿保乡", kind="svr")[0])

    # --- 角色名（含等级数字前缀） ---
    check("第五雨薇 ↔ '60 第五雨薇' 命中", match_name("第五雨薇", "60 第五雨薇", kind="character")[0])
    check("第五雨薇 ↔ 第五雨薇 命中", match_name("第五雨薇", "第五雨薇", kind="character")[0])

    # --- find_best_match：取最高分而非首个命中 ---
    idx, score = find_best_match("狐梦桃乡", ["愿保乡", "狐梦桃乡", "狐梦桃"], kind="svr")
    check(f"find_best_match 返回精确项下标1 (idx={idx}, score={score:.2f})", idx == 1)
    idx, score = find_best_match("愿保乡", ["愿保乡X", "愿保乡"], kind="svr")
    check(f"find_best_match 完全相等者优先 (idx={idx})", idx == 1)
    idx, score = find_best_match("狐梦桃乡", ["愿保乡"], kind="svr")
    check(f"全部低于阈值返回 -1 (idx={idx})", idx == -1)

    # --- 单字符噪声只认全等 ---
    # 注：纯数字文本会被 character 归一化剥成空串（等级前缀语义），故用汉字测单字符
    check("角色名 '雪' ↔ '雪' 命中（全等）", match_name("雪", "雪", kind="character")[0])
    check("角色名 '雪' ↔ '月' 不命中", not match_name("雪", "月", kind="character")[0])
    check("纯数字 '60' 作为角色名归一为空 → 不命中", not match_name("60", "60", kind="character")[0])

    logger.info("-" * 40)
    if failures:
        logger.info(f"自检失败 {len(failures)} 项：")
        for f in failures:
            logger.info(f"  - {f}")
        raise SystemExit(1)
    logger.info("fuzzy_match.py 全部自检通过")
