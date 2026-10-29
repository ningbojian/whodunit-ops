"""全局剧名记忆：把「用户怎么拼这个名字」映射到公共库条目。

为什么要有它：公共库 5809 行里 ``aliases`` **全是空的**——爬虫不产别名，
导入 CLI 也只能填空白单元格。而店长打字的口径千差万别，「病娇男孩的精分
曰记」错一个字就搜不到。这里只沉淀一条事实：「他这么拼 → 确认是哪个本」，
下次任何门店再这么拼就能直接命中。

边界（决定了这个模块长什么样）：

  - **只指向公共库**：``script_id`` 必须真实存在于当前公共库帧中。私有剧本
    id 永不入库，否则等于把 A 店的私有档案标识暴露给 B 店。
  - **只接受与目标本名足够相近的拼写**（见 :func:`_validate_key`）。因此
    这张表表达不了任意文本，也就当不了 PII 或经营数据的草稿纸。
  - **有歧义的说法拒绝记录**：同一串字同时像好几个剧名时（系列本、只差一
    个字的两个本），先写者胜会让第一个确认它的门店把所有人永久钉在那一条
    上。这类说法不进表。
  - **只在店长确认全名后**由 ``script_library(action="remember")`` 写入。
  - 读路径由 ``find_scripts`` 在「正常匹配为空、或只剩 loose 弱匹配」时
    点查一次，不做缓存——缓存会引入多 worker 陈旧问题，而 miss 是低频路径。

数据库与 Excel 两种后端行为一致：Excel 模式没有全局表，退化成本地 JSON
sidecar，做法与 ``data/search_usage_daily.json`` 相同。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from app.tools.scripts import normalize_script_name
from app.tools.storage import is_missing_value

logger = logging.getLogger(__name__)

# 记录下来的说法最多这么长。真实剧名最长 24 字，这里再宽一点。
_MAX_QUERY_KEY = 32

# 全局表的行数上限。这张表是「共享记忆」，不是存储。没有上限时，一个已获批
# 的账号可以一次一个字地攒进来，每一行单独看都合规，连起来就是任意文本。
# 达到上限后新说法只是不再沉淀，检索仍会走 bigram 建议。
_MAX_ROWS_TOTAL = 5000
_MAX_ROWS_PER_TARGET = 8

_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_WXID_RE = re.compile(r"wxid_", re.IGNORECASE)

# Unicode 大类里的「不可见 / 不可打印」：控制符、格式符、代理对、私用区、
# 未分配码位。用 ``[\x00-\x1f\x7f]`` 这类区间不够——C1 控制符
# （U+0080–U+009F）、零宽连接符、RTL OVERRIDE 全落在区间外，而 RTL
# OVERRIDE 能让存下去的说法在显示上完全变成另一个样子。
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})

# 大类拦不住的一批「渲染为空的字符」：它们不是 C 类，但屏幕上什么都不显示。
# 不拦的后果很具体——同一句真名能刷出无数条肉眼一致、码位不同的键，把每个
# 剧本 8 条的说法配额占满，而它们谁都不会被真正搜到。
# 组合用附加符（U+0301 之类）**不在这里**：那是看得见的变音符号，有些本名
# 真的会用，一刀切会把它们连同错拼一起误杀。
#
# 这张表是**枚举**出来的，不是想到哪写到哪：拿 ``unicodedata.name()`` 里含
# FILLER / BLANK / NULL / INVISIBLE / ZERO WIDTH / SOFT HYPHEN / REPLACEMENT
# CHARACTER / VARIATION SELECTOR 的码位扫一遍全平面，减去 C 类已经拦掉的，
# 剩下的就是下面这些。新增 Unicode 版本时可照同样办法再扫一次：
#
#   for cp in range(0x110000):
#       name = unicodedata.name(chr(cp), "")
#       if any(k in name for k in KEYWORDS) and not _has_invisible(chr(cp)): ...
_INVISIBLE_CODEPOINTS = (
    (0x00AD, 0x00AD),  # SOFT HYPHEN
    (0x034F, 0x034F),  # COMBINING GRAPHEME JOINER
    (0x115F, 0x1160),  # HANGUL CHOSEONG / JUNGSEONG FILLER
    (0x17B4, 0x17B5),  # KHMER VOWEL INHERENT AQ / AA
    (0x1806, 0x1806),  # MONGOLIAN TODO SOFT HYPHEN
    (0x180B, 0x180F),  # MONGOLIAN FREE VARIATION SELECTOR 1–4 / MVS
    (0x2400, 0x2400),  # SYMBOL FOR NULL
    (0x2422, 0x2422),  # BLANK SYMBOL
    (0x2800, 0x2800),  # BRAILLE PATTERN BLANK
    (0x3164, 0x3164),  # HANGUL FILLER
    (0xA8F9, 0xA8F9),  # DEVANAGARI GAP FILLER
    (0xFE00, 0xFE0F),  # VARIATION SELECTOR 1–16
    (0xFFA0, 0xFFA0),  # HALFWIDTH HANGUL FILLER
    (0xFFFC, 0xFFFD),  # OBJECT REPLACEMENT CHARACTER / REPLACEMENT CHARACTER
    (0x10AF6, 0x10AF6),  # MANICHAEAN PUNCTUATION LINE FILLER
    (0x1144E, 0x1144E),  # NEWA GAP FILLER
    (0x11945, 0x11945),  # DIVES AKURU GAP FILLER
    (0x11C44, 0x11C45),  # BHAIKSUKI GAP FILLER-1 / -2
    (0x11F48, 0x11F48),  # KAWI PUNCTUATION SPACE FILLER
    (0x13440, 0x13442),  # EGYPTIAN HIEROGLYPH MIRROR HORIZONTALLY / FULL / HALF BLANK
    (0x16FE4, 0x16FE4),  # KHITAN SMALL SCRIPT FILLER
    (0x1D159, 0x1D159),  # MUSICAL SYMBOL NULL NOTEHEAD
    (0xE0100, 0xE01EF),  # VARIATION SELECTOR 17–256
)


class AliasRejected(ValueError):
    """写入被规则拒绝。``code`` 直接透传给 Agent 工具的错误码。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class AliasUnavailable(RuntimeError):
    """记忆后端写失败。读路径不会抛这个——查不到就当没有别名。"""


def _name_key(value: Any) -> str:
    """与 ``app.tools.scripts._name_key`` 逐字一致：归一化后 casefold。"""
    return normalize_script_name(value).casefold()


def _has_invisible(text: str) -> bool:
    """是否含控制符 / 格式符 / 代理对 / 私用区 / 渲染为空的填充字符。

    必须在**归一化之前**对原始输入判断：``" ".join(text.split())`` 会把
    ``\t\n\v\f\r\x1c-\x1f`` 折叠成空格，等归一化完再查，这些字符早就没了。
    """
    for char in text:
        if unicodedata.category(char) in _INVISIBLE_CATEGORIES:
            return True
        code = ord(char)
        for low, high in _INVISIBLE_CODEPOINTS:
            if low <= code <= high:
                return True
    return False


def _marks(text: str) -> set[str]:
    """文本里的组合附加符（Unicode 大类 M）。"""
    return {char for char in text if unicodedata.category(char)[0] == "M"}


def _number_sequence(text: str) -> str:
    """按顺序取出文本里的数字字符。

    不能用 ``re`` 的 ``\\d``：它只匹配 Nd（十进制数字），①⑦³ 这些 No 类
    数字整类漏掉——「年轮①」和「年轮1」是同一类编号。
    """
    return "".join(char for char in text if unicodedata.category(char)[0] == "N")


def _non_number_sequence(text: str) -> str:
    """按顺序取出文本里的非数字字符，即数字之外的「骨架」。"""
    return "".join(char for char in text if unicodedata.category(char)[0] != "N")


def _number_frame_ok(key_frame: str, target_frame: str) -> bool:
    """数字对得上之后，再看非数字骨架是不是也还是同一个名字。

    只比数字串会放行一类很具体的错：库里真有《1440》《8/16》《4149》这类
    数字剧名，店长说「￥1440」「14:40」「8月16」时数字串仍然是 1440 / 816，
    而 ``_aligns_with_title`` 容许一个字以内的差异——于是报价和场次时间会
    被当成「另一种拼写」永久写进全局表。所以骨架也必须对齐：

    - 目标名是纯数字：键也只能是数字，多一个符号都不行；
    - 目标的骨架里没有字母汉字（只有 ``/`` ``%`` 这类分隔符）：要求逐字
      相同，不给容错预算，否则 ``8月16`` 能冒充 ``8/16``；
    - 其余：按名字对齐规则（连续子串、至多差一个字），这样数字夹在真名
      中间时（``青楼3月下美人``）打错一个字仍然学得进去。
    """
    if not target_frame:
        return not key_frame
    if not key_frame:
        return False
    if not any(unicodedata.category(char)[0] == "L" for char in target_frame):
        return key_frame == target_frame
    return _aligns_with_title(key_frame, target_frame)


def _levenshtein(left: str, right: str) -> int:
    """编辑距离。两个串都在 32 字以内，O(n·m) 足够。"""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, ch_left in enumerate(left, start=1):
        current = [i]
        for j, ch_right in enumerate(right, start=1):
            cost = 0 if ch_left == ch_right else 1
            current.append(
                min(
                    previous[j] + 1,  # 删除
                    current[j - 1] + 1,  # 插入
                    previous[j - 1] + cost,  # 替换
                )
            )
        previous = current
    return previous[-1]


def _aligns_with_title(key: str, target_key: str) -> bool:
    """``key`` 能否对齐到目标名的**某一段连续子串**（长度差 ≤1、编辑距离 ≤1）。

    为什么是「连续子串」而不是「整体」。店长打的口径经常是简称：8 字的
    《金陵有座东君书院》他会写成「东君书院」。简称本身能被正常检索的
    包含匹配捞到，不会走到这里；但简称**再带一个错字**（「东君书阮」）就
    会 miss——那正是最值得记下来的一条。要求整体对齐会把这类全拒掉。

    连续子串这条约束同时守住了边界：键必须几乎逐字出现在标题的某个位置，
    最多夹带一个外来字，所以这张表仍然表达不了任意文本。
    """
    for window_len in (len(key) - 1, len(key), len(key) + 1):
        if window_len <= 0 or window_len > len(target_key):
            continue
        for start in range(len(target_key) - window_len + 1):
            if _levenshtein(key, target_key[start : start + window_len]) <= 1:
                return True
    return False


def _spelling_ok(key: str, target_key: str) -> bool:
    """``key`` 够不够格当 ``target_key`` 的另一种写法。

    三道口径按目标名长短分开，原因不同：

    - 目标 2–3 字：只允许「等长 + 至多改一个字」。
    - 目标 ≥4 字、键 <4 字：键必须是原名的**逐字连续一段**，不允许带错字。
      短键的信息量太小，允许它带一个错字就等于「三个字里一个字可以随便
      填」——「好的吧」「老板说」这类日常对话会被当成某个两字剧名加一个字
      收进来。代价是「格林林」→《格林格林》这种 3 字错拼学不进去，那类
      说法仍会拿到 bigram 建议。
    - 其余：连续子串对齐（允许一个错字 / 多一个字 / 少一个字）。
    """
    if len(target_key) <= 3:
        return len(key) == len(target_key) and _levenshtein(key, target_key) <= 1
    if len(key) < 4:
        return key in target_key
    return _aligns_with_title(key, target_key)


def _ambiguous_with(
    key: str,
    target_key: str,
    other_keys: set[str],
) -> str | None:
    """除了目标名，这个说法还像哪个剧名？没有则返回 None。

    为什么必须拦：映射是全局的，先写者胜。一个说法同时像《东京恐怖故事：
    加藤病院》和《东京恐怖故事：如月车站》时，谁先确认「东京恐怖故是」，
    全站就永久绑定到谁那一条。这类说法只有当事门店能分辨，不该由先写者
    替所有人决定。

    只对 3 字及以上的说法调用，2 字说法由调用方豁免（理由见 ``_validate_key``）。

    成本控制：4 字以上的键要跑编辑距离，先用「不同字数差超过 2 就直接
    跳过」预筛——编辑距离 ≤1 的串不可能有 3 个以上不同的字。
    """
    distinct = set(key) if len(key) >= 4 else None
    for other in other_keys:
        if not other or other == target_key:
            continue
        if distinct is not None and len(other) > 3:
            if len(distinct & set(other)) < len(distinct) - 2:
                continue
        if _spelling_ok(key, other):
            return other
    return None


def _validate_key(
    key: str,
    *,
    target_key: str,
    other_keys: set[str],
) -> None:
    """只允许「目标剧名的一种写法」通过。

    这道检查是整张表的诚信基础：过不了它，就不能把用户输入写进全局。
    判据是对齐（必须落在标题的某段连续子串上，最多差一个字），**不是**
    字面重合度。

    为什么重合度不行：在真实公共库上实测，``东君书院晚场``（目标是《金陵有座
    东君书院》）、``张三要玩病娇男孩的精``、``王小明住在杭州市西湖区练习时长
    两年半的魔法是用来`` 的 bigram 重合度都在 0.5 以上——名字前面挂一整个客户
    名、后面挂「晚场 / 半价」，重合度几乎不变。重合度只回答「像不像」，对
    「像 + 多出来一段任意文本」没有分辨力；对齐有。
    """
    if not key:
        raise AliasRejected("empty_query", "要记录的说法不能为空。")

    # 敏感形态必须排在长度检查之前：否则「13800138000」会先被长度挡掉，
    # 返回 not_similar，错误码失去诊断价值。
    if "@" in key or _URL_RE.search(key) or _WXID_RE.search(key):
        raise AliasRejected("sensitive_query", "这个说法含联系方式或链接，不予记录。")

    # 连续 11 位只是最常见写法；「138 0013 8000」「138-0013-8000」同样要拦，
    # 所以看总位数。目标本名自己就带很多数字时按它的位数放宽。
    key_numbers = _number_sequence(key)
    target_numbers = _number_sequence(target_key)
    if len(key_numbers) > max(6, len(target_numbers)):
        raise AliasRejected("sensitive_query", "这个说法含数字编号，不予记录。")

    if len(key) > _MAX_QUERY_KEY:
        raise AliasRejected("not_similar", "这个说法和目标剧名差得太多，不予记录。")

    # 数字必须逐个对得上。《8/16》写成「16号」、目标没有数字却写成「青楼138」，
    # 都是把价格 / 场次 / 编号塞进来的典型形态。目标名自己带数字时不能改成
    # 别的数字——那是另一个本。
    if key_numbers and key_numbers != target_numbers:
        if not target_numbers:
            raise AliasRejected("sensitive_query", "这个说法混入了数字，不予记录。")
        raise AliasRejected("not_similar", "这个说法的编号和目标剧名对不上，不予记录。")

    # 数字串对上还不够：《1440》写成「￥1440」「14:40」时数字串也是 1440，
    # 只比数字会把报价和场次时间放进来。骨架也要对得上。
    if key_numbers and not _number_frame_ok(
        _non_number_sequence(key), _non_number_sequence(target_key)
    ):
        raise AliasRejected(
            "not_similar",
            "这个说法在数字前后多了符号或单位，和目标剧名的写法对不上，不予记录。",
        )

    if len(target_key) < 2:
        raise AliasRejected(
            "target_too_short", "单字剧名无法校验拼写差异，不予记录。"
        )
    if len(key) < 2:
        raise AliasRejected("not_similar", "单个字无法作为剧名说法，不予记录。")
    if key == target_key:
        raise AliasRejected("redundant", "这个说法就是剧名本身，无需记录。")
    # 若该说法已是别的剧名，记下来会永久污染对那个本的所有查询。
    if key in other_keys:
        raise AliasRejected(
            "collides_with_other", "这个说法已经是另一个剧本的剧名，不予记录。"
        )

    if not _spelling_ok(key, target_key):
        raise AliasRejected("not_similar", "这个说法和目标剧名差得太多，不予记录。")

    # 对得上目标名还不够：同一个说法若能当**另一个**公共剧名的写法，记下来
    # 就把所有人钉在这一条上（映射是全局的，先写者胜）。这类说法只有当
    # 事门店分得清，不该由先写者替所有人决定。
    #
    # 2 字说法不做这一档，这是量出来的取舍：真实库里有 7 个以「青」开头的
    # 两字本（青楼 / 青白 / 青丝 / 青沙 / 青史 / 青枝 / 青语），任何一个
    # 错字都同时「像」它们全部，歧义判定会把两字名的错拼**全部**拒掉——
    # 而两字查询恰恰是 bigram 兜底明确不覆盖的一段（《青搂》《青楼》零交集），
    # 全局记忆是它唯一的救兵。代价是「拼场」这种日常词也可能被确认成
    # 《焚场》的一条说法；它只指向公共库、读出来带「来自其他门店」的提示、
    # 影响范围是一条两字键，比整段功能失效可接受。
    if len(key) >= 3 and _ambiguous_with(key, target_key, other_keys) is not None:
        raise AliasRejected(
            "ambiguous_query",
            "这个说法也能当成另一个剧本的写法，记下来会让其他人搜到错的本，不予记录。",
        )


def prepare_alias(
    raw_query: Any,
    script_id: Any,
    public_frame: pd.DataFrame | None,
) -> tuple[str, str]:
    """校验并返回 ``(query_key, public_script_id)``；不合法就抛 AliasRejected。

    校验放在 store 层而不是只放 Agent 工具层，是为了让任何未来的调用方
    都绕不过去。
    """
    frame = public_frame
    if frame is None or getattr(frame, "empty", True):
        raise AliasRejected(
            "public_library_unavailable", "公共剧本库不可用，暂不能记录映射。"
        )
    if "script_id" not in frame.columns or "script_name" not in frame.columns:
        raise AliasRejected(
            "public_library_unavailable", "公共剧本库缺少必要列。"
        )

    # 不可见字符要在归一化之前查：空白折叠会把 \t\n\v\f\r\x1c-\x1f 变成
    # 空格，查完全归一化的键就再也看不到它们了。首尾空白不算问题。
    raw_text = "" if is_missing_value(raw_query) else str(raw_query).strip()
    if _has_invisible(raw_text):
        raise AliasRejected(
            "sensitive_query", "这个说法含不可见字符，不予记录。"
        )

    # pd.NA / NaN 不能走 ``script_id or ""``：布尔求值会直接抛 TypeError。
    target = "" if is_missing_value(script_id) else str(script_id).strip()
    if not target:
        raise AliasRejected("bad_request", "需要指定要记录的公共库剧本。")

    ids = frame["script_id"].astype("string").fillna("").tolist()
    try:
        position = ids.index(target)
    except ValueError:
        raise AliasRejected(
            "script_not_public", "只能记录公共库剧本的说法。"
        ) from None

    target_key = _name_key(frame["script_name"].tolist()[position])
    if not target_key:
        raise AliasRejected("target_too_short", "目标剧本没有名字，无法校验。")

    other_keys = {_name_key(name) for name in frame["script_name"].tolist()}
    other_keys.discard(target_key)

    key = _name_key(raw_text)
    # casefold 会**引入**组合符（"İ".casefold() 是 "i" + U+0307），而上面那道
    # 检查跑在 casefold 之前。归一化本身只折叠空白和截断，不会凭空造出组合
    # 符，所以「归一化后多出来的组合符」一定是 casefold 带进来的。
    if _marks(key) - _marks(raw_text):
        raise AliasRejected(
            "sensitive_query", "这个说法含不可见字符，不予记录。"
        )
    _validate_key(key, target_key=target_key, other_keys=other_keys)
    return key, target


def _public_ids(public_frame: pd.DataFrame | None) -> set[str]:
    """当前公共库里真实存在的 ``script_id`` 集合。

    用来判断一条旧映射是不是**悬空**的：公共库每次导入都是整表删除再重插
    （``SqlPublicLibrary._write_all``），旧 id 会随改名 / 下架消失。悬空的
    键读路径按「没学到」处理（见 ``_learned_script``），所以它占着位置却
    谁都帮不上。
    """
    if public_frame is None or getattr(public_frame, "empty", True):
        return set()
    if "script_id" not in public_frame.columns:
        return set()
    return {
        str(value)
        for value in public_frame["script_id"].tolist()
        if not is_missing_value(value)
    }


def _result_payload(key: str, target: str, created: bool) -> dict[str, Any]:
    """写入结果。

    ``created=False`` 时不回传 ``script_id``：表里那一条是别处先写下的。
    把调用方的入参原样回显，模型会以为它记下的就是这条——既误导对话，也
    等于用回显掩盖了真实的存储状态。

    ``created=False`` 有两种来源，都不适合回显：同一个说法写了两次；或
    2 字说法已被别的门店绑到另一个本（2 字键不做歧义判断，理由见
    ``_validate_key``）。两种情况调用方拿到的都只是「已经有了」。
    """
    if not created:
        return {"query_key": key, "created": False}
    return {"query_key": key, "script_id": target, "created": True}


class SqlScriptAliasMemory:
    """数据库后端：``script_aliases`` 全局表（无 tenant_id）。"""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        # 这把锁只串行化**写入**。读是纯 SELECT，数据库自己保证一致性；
        # 给读加进程锁的代价是一次慢查询（跨境连接超时可达 10s）会挡住
        # 所有进程内的对话。
        self._write_lock = threading.RLock()

    def lookup(self, query_key: str) -> str | None:
        from sqlalchemy import text

        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT script_id FROM script_aliases "
                    "WHERE query_key = :key"
                ),
                {"key": str(query_key)},
            ).fetchone()
        return str(row[0]) if row else None

    def remember(
        self,
        raw_query: Any,
        script_id: Any,
        *,
        public_frame: pd.DataFrame | None = None,
    ) -> dict[str, Any]:
        key, target = prepare_alias(raw_query, script_id, public_frame)
        valid_ids = _public_ids(public_frame)
        # 与 SqlSearchUsage 同一写法：原始 text() 不经过类型绑定，所以显式
        # 写成 naive UTC 字符串，表上的列也声明成 DateTime(timezone=False)。
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._write_lock:
                if self.engine.dialect.name == "sqlite":
                    # SQLite 没有 advisory lock，进程内再用一把模块级锁把
                    # 「聚合 + 插入」串起来。跨进程的 SQLite 并发写不是支持
                    # 的拓扑（生产是 Postgres，CLI 走 Excel），见文档。
                    with _SQLITE_WRITE_LOCK:
                        created = self._insert(key, target, now, valid_ids)
                else:
                    created = self._insert(key, target, now, valid_ids)
        except AliasRejected:
            # 配额拒绝要原样传给调用方；下面的兜底会把它变成「后端不可用」。
            raise
        except Exception as exc:  # noqa: BLE001 - 写失败只降级，不影响对话
            logger.warning("script alias write failed: %s", type(exc).__name__)
            raise AliasUnavailable("剧名记忆暂时不可用，请稍后再试。") from None
        return _result_payload(key, target, created)

    def _insert(self, key: str, target: str, now: str, valid_ids: set[str]) -> bool:
        """配额判断与插入放在同一个事务里，并先拿一把全局锁。

        只靠 ``COUNT(*)`` 再 ``INSERT`` 是不够的：两个写者各拿一个
        ``SqlScriptAliasMemory`` 实例时，各自看到的是插入前的计数，两边都
        通过配额检查，上限就被突破一条。Postgres 用事务级 advisory lock
        把这段串起来（锁随 COMMIT 释放，事务内的计数因此仍然有效）；
        表是全局的，所以锁键是固定常量而不是按租户。

        ``valid_ids`` 是当前公共库里真实存在的 id。已有的键如果指向一条
        已经不存在的公共行，那条映射读出来等于没学到，却会：
        (1) 让店长这次合法的确认被静默丢弃（回「此前已有记录」）；
        (2) 永久占用一行全局配额（``_MAX_ROWS_TOTAL``）。
        所以悬空键重新指向本次的目标；指向仍然存在的行的键保持「先写者胜」。
        """
        from sqlalchemy import text

        with self.engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                conn.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": _ALIAS_ADVISORY_LOCK_KEY},
                )
            # 一次往返取三个数：总行数、这个剧本已有几条、这条键当前指向哪里
            # （空串 = 还没有这条键）。已存在的键不占新名额，所以不查总上限——
            # 否则第二次确认同一个说法会莫名报「记忆已满」。
            counts = conn.execute(
                text(
                    "SELECT COUNT(*), "
                    "COALESCE(SUM(CASE WHEN script_id = :sid "
                    "THEN 1 ELSE 0 END), 0), "
                    "COALESCE(MAX(CASE WHEN query_key = :key "
                    "THEN script_id END), '') "
                    "FROM script_aliases"
                ),
                {"sid": target, "key": key},
            ).one()
            total, same_target, stored = (
                int(counts[0] or 0),
                int(counts[1] or 0),
                str(counts[2] or ""),
            )
            if stored:
                if stored in valid_ids:
                    return False
                _check_target_quota(same_target)
                result = conn.execute(
                    text(
                        "UPDATE script_aliases SET script_id = :sid, "
                        "updated_at = :now "
                        "WHERE query_key = :key AND script_id = :old"
                    ),
                    {"sid": target, "old": stored, "key": key, "now": now},
                )
                return bool(result.rowcount)
            _check_quota(total, same_target)
            result = conn.execute(
                text(
                    "INSERT INTO script_aliases"
                    "(query_key, script_id, created_at, updated_at) "
                    "VALUES (:key, :sid, :now, :now) "
                    "ON CONFLICT (query_key) DO NOTHING"
                ),
                {"key": key, "sid": target, "now": now},
            )
            return bool(result.rowcount)

    def close(self) -> None:
        return None


# Postgres 事务级 advisory lock 的固定键。表是全局的，所以不需要按租户或
# 按剧本区分；用固定常量是为了让所有实例抢同一把锁。
_ALIAS_ADVISORY_LOCK_KEY = 0x736372616C696173
# SQLite 没有 advisory lock，进程内用这把模块级锁串行化「计数 + 插入」。
_SQLITE_WRITE_LOCK = threading.RLock()


def _check_quota(total: int, same_target: int) -> None:
    """行数上限。超过就拒写，但检索不受影响。"""
    if total >= _MAX_ROWS_TOTAL:
        raise AliasRejected(
            "alias_quota_exceeded",
            "共享剧名记忆已达上限，本次未记录；检索仍会给出字面相近的候选。",
        )
    _check_target_quota(same_target)


def _check_target_quota(same_target: int) -> None:
    """单个剧本的说法上限。

    与总上限分开，是因为「把一条悬空键重新指向」不增加总行数，却会增加这个
    剧本的行数——那种场合只该查这一条。
    """
    if same_target >= _MAX_ROWS_PER_TARGET:
        raise AliasRejected(
            "alias_quota_exceeded",
            "这个剧本已记下足够多的说法，本次未记录。",
        )


_JSON_LOCKS: dict[str, threading.RLock] = {}
_JSON_LOCKS_GUARD = threading.Lock()


def _json_lock(path: Path) -> threading.RLock:
    """按文件的绝对路径共享一把锁。

    锁挂在实例上不够：同一个 ``data/script_aliases.json`` 会被网页进程和
    CLI 各开一个实例，实例锁保护不了对方。跨进程的互斥由
    :func:`_sidecar_guard` 负责；这把锁只省掉同进程内的锁文件开销。

    键要过 ``normcase``：Windows 上 ``Path.resolve()`` 只有在文件已存在时
    才把大小写折成真实拼写，同一文件的两条不同大小写路径会落到两个条目上。
    """
    token = os.path.normcase(str(path.resolve()))
    with _JSON_LOCKS_GUARD:
        lock = _JSON_LOCKS.get(token)
        if lock is None:
            lock = _JSON_LOCKS[token] = threading.RLock()
        return lock


@contextmanager
def _sidecar_guard(path: Path) -> Iterator[None]:
    """sidecar 的**跨进程**互斥锁。

    为什么必须有：写路径是「读全表 → 改一条 → 整文件替换」。进程内的
    RLock 挡不住网页进程和 CLI 同时写；临时文件名带 pid 只保证两个写者
    不共用同一个临时文件，保证不了「读到的表还是最新的」。两个写者都读
    到旧表时，后替换的那份会把先写的键**静默**丢掉，而两个调用方都会
    收到「已记录」。

    实现是相邻的一个 ``.lock`` 文件 + 操作系统建议锁，失败就抛 OSError，
    由调用方降级成 ``AliasUnavailable``——宁可这次不记，也不静默覆盖。
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")  # noqa: SIM115 - 需要跨平台按 fd 加锁
    try:
        _lock_file(handle)
        try:
            yield
        finally:
            _unlock_file(handle)
    finally:
        handle.close()


def _lock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        # LK_LOCK：锁不上时每 1 秒重试一次，共 10 次，然后抛 OSError。
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:  # 关闭句柄本身也会释放；不要因此把成功写入变成失败
        logger.warning("script alias lock release failed")


class JsonScriptAliasMemory:
    """Excel 模式后端：``data/script_aliases.json``。

    写入频率极低（一次确认才写一次），整文件原子替换即可。读失败一律
    当 ``{}``——记忆不可用不能连累搜索和对话。但**写**之前读不出来时必须
    失败：整表重写会把读不到的那份文件直接覆盖掉，等于悄悄清空所有记忆。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = _json_lock(self.path)

    def _load(self, *, strict: bool = False) -> dict[str, str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            if strict:
                logger.warning(
                    "script alias sidecar unreadable: %s", type(exc).__name__
                )
                raise AliasUnavailable(
                    "剧名记忆文件读不出来，为避免覆盖已有记录，本次没有写入。"
                ) from None
            return {}
        if not isinstance(raw, dict):
            if strict:
                raise AliasUnavailable(
                    "剧名记忆文件格式不对，为避免覆盖已有记录，本次没有写入。"
                )
            return {}
        return {
            str(key): str(value)
            for key, value in raw.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def lookup(self, query_key: str) -> str | None:
        """点查一次。任何失败都返回 None——记忆不可用不能连累搜索。

        读也走 :func:`_sidecar_guard` 不是多余的：Windows 上 ``os.replace``
        需要目标文件没有被任何句柄打开（CPython 的 ``open`` 不带
        ``FILE_SHARE_DELETE``），一次读正好撞上替换，写侧就会
        ``PermissionError``、这次确认整条落空。让读也排队，冲突就不存在。
        代价是 miss 路径上偶尔等一次写（毫秒级），而写只在店长确认时发生。
        """
        with self._lock:
            try:
                with _sidecar_guard(self.path):
                    data = self._load()
            except OSError as exc:
                logger.warning(
                    "script alias sidecar locked: %s", type(exc).__name__
                )
                return None
            return data.get(str(query_key))

    def remember(
        self,
        raw_query: Any,
        script_id: Any,
        *,
        public_frame: pd.DataFrame | None = None,
    ) -> dict[str, Any]:
        key, target = prepare_alias(raw_query, script_id, public_frame)
        valid_ids = _public_ids(public_frame)
        with self._lock:
            try:
                # 读、配额判断、改、整文件替换必须在**同一段**临界区里，
                # 而且要跨进程——否则两个写者都读到旧表，后写的把先写的
                # 键静默覆盖掉，两边却都回「已记录」。
                with _sidecar_guard(self.path):
                    data = self._load(strict=True)
                    stored = data.get(key)
                    if stored is not None:
                        if stored in valid_ids:
                            return _result_payload(key, target, False)
                        # 悬空映射：旧目标已不在公共库，读路径本来就当它不存在。
                        # 让它指向这次确认的本，而不是把这次确认丢掉。
                        same_target = sum(
                            1 for value in data.values() if value == target
                        )
                        _check_target_quota(same_target)
                        data[key] = target
                        self._write(data)
                        return _result_payload(key, target, True)
                    _check_quota(
                        len(data),
                        sum(1 for value in data.values() if value == target),
                    )
                    data[key] = target
                    self._write(data)
            except AliasRejected:
                raise
            except (OSError, UnicodeError) as exc:
                logger.warning(
                    "script alias sidecar write failed: %s", type(exc).__name__
                )
                raise AliasUnavailable(
                    "剧名记忆暂时不可用，请稍后再试。"
                ) from None
        return _result_payload(key, target, True)

    def _write(self, data: dict[str, str]) -> None:
        """整文件原子替换。临时文件名带 pid 和线程号，避免两个写者共用。"""
        tmp = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def close(self) -> None:
        return None


def build_script_aliases(
    *,
    uses_database: bool,
    engine: Any = None,
    data_dir: Any = None,
) -> Any:
    """按后端选实现。数据库模式必须有 engine，否则退化为无记忆。

    Excel 模式没有全局表，落 ``data/script_aliases.json``——与
    ``build_search_usage`` 的 sidecar 取舍一致。
    """
    if uses_database:
        if engine is None:
            return None
        return SqlScriptAliasMemory(engine)
    if data_dir is None:
        from app.config import DATA_DIR

        data_dir = DATA_DIR
    return JsonScriptAliasMemory(Path(data_dir) / "script_aliases.json")


__all__ = [
    "AliasRejected",
    "AliasUnavailable",
    "JsonScriptAliasMemory",
    "SqlScriptAliasMemory",
    "build_script_aliases",
    "prepare_alias",
]
