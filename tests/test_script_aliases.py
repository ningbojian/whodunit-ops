"""全局剧名记忆：校验规则、跨租户生效、只读路径、两种后端。

这个模块的每一个测试都在守同一条线：``script_aliases`` 只能表达
「这个剧本还能怎么拼」。任何一条校验被放宽，公共库就变成了任意文本
的共享存储——而 Demo 是公开 URL、没有隐私告知。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text

from app.agent import AgentDeps, build_store_tools
from app.store.engine import create_database_engine, ensure_schema
from app.store.script_aliases import (
    AliasRejected,
    AliasUnavailable,
    JsonScriptAliasMemory,
    SqlScriptAliasMemory,
    build_script_aliases,
    prepare_alias,
)
from app.store.sql import SqlStore
from app.tools.scripts import find_scripts

NOW = pd.Timestamp("2026-10-20 18:00")

PUBLIC = pd.DataFrame(
    {
        "script_id": ["pub-nianlun", "pub-qinglou", "pub-bjnh"],
        "script_name": ["年轮", "青楼", "病娇男孩的精分日记"],
        "aliases": [None, None, None],
        "script_type": ["情感", "推理", "恐怖"],
        "total_seats": [6, 7, 6],
        "duration_minutes": [240, 300, 300],
        "list_price": [168, 188, 198],
        "notes": [None, None, None],
        "source_text": [None, None, None],
        "created_at": [None, None, None],
        "updated_at": [None, None, None],
    }
)


@pytest.fixture()
def engine(tmp_path):
    value = create_database_engine(sqlite_path=tmp_path / "store.sqlite3")
    ensure_schema(value)
    yield value
    value.dispose()


def _store(engine, tenant_id: str) -> SqlStore:
    store = SqlStore(engine, tenant_id)
    store.ensure_tenant()
    return store


def _counting(engine, tenant_id: str) -> tuple[AgentDeps, SqlScriptAliasMemory]:
    return (
        AgentDeps(
            store=_store(engine, tenant_id),
            now=lambda: NOW,
            public_scripts=PUBLIC,
            script_aliases=SqlScriptAliasMemory(engine),
        ),
        SqlScriptAliasMemory(engine),
    )


def _call(deps: AgentDeps, args: dict) -> dict:
    tools = {item.name: item for item in build_store_tools(deps)}
    return json.loads(tools["script_library"].invoke(args))


def _rows(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM script_aliases")).scalar_one()


# ---------------------------------------------------------------------------
# 一、跨租户：A 店确认过的说法，B 店能直接用
# ---------------------------------------------------------------------------


def test_learned_alias_helps_another_tenant(engine):
    """A 店确认「年伦」=《年轮》；B 店拿新实例也能搜到。

    用**新建的** memory 实例读，证明是持久化而不是对象状态。
    """
    deps_a, _ = _counting(engine, "tenant-a")
    written = _call(
        deps_a, {"action": "remember", "query": "年伦", "script_id": "pub-nianlun"}
    )
    assert written["ok"] is True, written
    assert written["data"]["created"] is True

    deps_b, memory_b = _counting(engine, "tenant-b")
    items = find_scripts(
        "年伦",
        store=deps_b.store,
        public=PUBLIC,
        sessions=pd.DataFrame(),
        limit=5,
        learned_aliases=memory_b,
    )
    assert [item["script_id"] for item in items] == ["pub-nianlun"]
    assert items[0]["match_quality"] == "learned"
    assert items[0]["source"] == "公共库"


def test_remember_by_script_name_resolves_public_row(engine):
    """模型手里通常只有名字：按名解析到公共行，且必须唯一。"""
    deps, _ = _counting(engine, "tenant-a")
    result = _call(
        deps,
        {"action": "remember", "query": "青搂", "script_name": "青楼"},
    )
    assert result["ok"] is True, result
    assert result["data"]["script_id"] == "pub-qinglou"


# ---------------------------------------------------------------------------
# 二、校验：这张表不能当草稿纸
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "13800138000",
        "a@b.com",
        "wxid_abc123",
        "https://example.com/年轮",
        "年轮\x07bell",
    ],
)
def test_rejects_contact_and_link_shaped_keys(query):
    with pytest.raises(AliasRejected) as info:
        prepare_alias(query, "pub-nianlun", PUBLIC)
    assert info.value.code == "sensitive_query"


@pytest.mark.parametrize(
    "query",
    ["年轮", "张三要玩年轮", "青楼李", "年轮2026", "今天有人拼年轮吗"],
)
def test_rejects_unrelated_or_redundant_keys(query):
    with pytest.raises(AliasRejected):
        prepare_alias(query, "pub-nianlun", PUBLIC)


def test_rejected_writes_leave_no_rows(engine):
    """拒绝必须发生在写库之前，不能靠事后清理。"""
    deps, _ = _counting(engine, "tenant-a")
    for query in ("13800138000", "张三要玩年轮", "a@b.com"):
        result = _call(
            deps, {"action": "remember", "query": query, "script_id": "pub-nianlun"}
        )
        assert result["ok"] is False, result
        assert result["error_code"] == "sensitive_query" or (
            result["error_code"] == "not_similar"
        )
    assert _rows(engine) == 0


@pytest.mark.parametrize(
    "script_id",
    ["draft-abc", "0123456789ab", "pub-deadbeef", ""],
)
def test_rejects_non_public_or_missing_target(script_id):
    """只看 pub- 前缀不够：必须在当前公共库帧里真的存在。"""
    with pytest.raises(AliasRejected) as info:
        prepare_alias("年伦", script_id, PUBLIC)
    assert info.value.code in {"bad_request", "script_not_public"}


def test_rejects_key_that_is_another_scripts_name():
    """把《年轮》映射到一个恰好叫《青楼》的说法，会永久污染所有人的搜索。"""
    with pytest.raises(AliasRejected) as info:
        prepare_alias("青楼", "pub-nianlun", PUBLIC)
    assert info.value.code == "collides_with_other"


def test_single_character_target_is_rejected():
    frame = pd.DataFrame({"script_id": ["pub-x"], "script_name": ["谜"]})
    with pytest.raises(AliasRejected) as info:
        prepare_alias("迷", "pub-x", frame)
    assert info.value.code == "target_too_short"


# ---------------------------------------------------------------------------
# 三、读路径：只在 miss 时查一次，且永不参与打分
# ---------------------------------------------------------------------------


class _CountingMemory:
    def __init__(self) -> None:
        self.calls = 0
        self.key = None

    def lookup(self, query_key: str) -> str | None:
        self.calls += 1
        self.key = query_key
        return "pub-nianlun"


def test_lookup_is_never_called_on_a_normal_match():
    memory = _CountingMemory()
    items = find_scripts(
        "年轮", public=PUBLIC, sessions=pd.DataFrame(), limit=5,
        learned_aliases=memory,
    )
    assert items and items[0]["match_quality"] == "exact"
    assert memory.calls == 0


def test_lookup_is_called_once_on_miss():
    memory = _CountingMemory()
    items = find_scripts(
        "年伦", public=PUBLIC, sessions=pd.DataFrame(), limit=5,
        learned_aliases=memory,
    )
    assert memory.calls == 1
    assert items[0]["match_quality"] == "learned"


def test_learned_hit_can_never_displace_a_real_exact_match():
    """学到的键不并进别名列表，所以它不可能靠排序顶掉真名。"""
    memory = _CountingMemory()
    items = find_scripts(
        "年轮", public=PUBLIC, sessions=pd.DataFrame(), limit=5,
        learned_aliases=memory,
    )
    assert [item["script_id"] for item in items] == ["pub-nianlun"]
    assert items[0]["match_quality"] == "exact"


def test_broken_memory_degrades_to_plain_search():
    class _Broken:
        def lookup(self, query_key):
            raise RuntimeError("db down")

    items = find_scripts(
        "年伦", public=PUBLIC, sessions=pd.DataFrame(), limit=5,
        learned_aliases=_Broken(),
    )
    # 记忆坏了只该「少一条线索」，不该让搜索报错。
    assert isinstance(items, list)
    assert all(item.get("match_quality") != "learned" for item in items)


def test_alias_pointing_at_a_vanished_public_row_is_ignored():
    """公共库重导后旧映射可能指向已不存在的行。

    那张表没有外键（导入是整表删了重插），所以这类悬空引用一定会有。
    读路径必须当「没学到」处理，不能抛错，也不能拿不到名字就乱猜。
    """

    class _Stale:
        def lookup(self, query_key):
            return "pub-deadbeef"

    items = find_scripts(
        "年伦", public=PUBLIC, sessions=pd.DataFrame(), limit=5,
        learned_aliases=_Stale(),
    )
    assert items == []



# ---------------------------------------------------------------------------
# 四、遮蔽：本店有同名私有档案时，私有档案赢
# ---------------------------------------------------------------------------


def test_private_row_shadows_the_learned_public_hit(engine):
    deps, memory = _counting(engine, "tenant-a")
    from app.tools.scripts import upsert_script

    upsert_script(
        script_name="年轮",
        script_type="情感",
        total_seats="6人",
        path=None,
        now=NOW,
        store=deps.store,
    )
    assert _call(
        deps, {"action": "remember", "query": "年伦", "script_id": "pub-nianlun"}
    )["ok"] is True

    items = find_scripts(
        "年伦",
        store=deps.store,
        public=PUBLIC,
        sessions=pd.DataFrame(),
        limit=5,
        learned_aliases=memory,
    )
    assert len(items) == 1
    # 返回的是本店私有档案，不是被它遮蔽的公共行。
    assert items[0]["source"] == "剧本库"
    assert not str(items[0]["script_id"]).startswith("pub-")


# ---------------------------------------------------------------------------
# 五、只读模式
# ---------------------------------------------------------------------------


def test_read_only_rejects_remember_without_writing(engine):
    deps, _ = _counting(engine, "tenant-a")
    readonly = AgentDeps(
        store=deps.store,
        now=lambda: NOW,
        public_scripts=PUBLIC,
        script_aliases=deps.script_aliases,
        read_only=True,
    )
    result = _call(
        readonly, {"action": "remember", "query": "年伦", "script_id": "pub-nianlun"}
    )
    assert result["ok"] is False
    assert result["error_code"] == "read_only"
    assert _rows(engine) == 0


def test_remember_without_memory_or_public_library_reports_clearly(engine):
    deps, _ = _counting(engine, "tenant-a")
    no_memory = AgentDeps(store=deps.store, now=lambda: NOW, public_scripts=PUBLIC)
    result = _call(
        no_memory, {"action": "remember", "query": "年伦", "script_id": "pub-nianlun"}
    )
    assert result["error_code"] == "alias_memory_unavailable"

    empty_public = AgentDeps(
        store=deps.store,
        now=lambda: NOW,
        public_scripts=PUBLIC.iloc[0:0],
        script_aliases=deps.script_aliases,
    )
    result = _call(
        empty_public,
        {"action": "remember", "query": "年伦", "script_id": "pub-nianlun"},
    )
    assert result["error_code"] == "public_library_unavailable"


def test_remember_by_name_reports_missing_columns_instead_of_raising(engine):
    """按名解析要读 script_name / script_id；缺列时应当是错误码，不是崩溃。

    工具层的异常兜底会把它变成 internal_error，可对模型来说「库坏了」和
    「库缺列」应当给同一个可操作的信号：换 script_id 或稍后再试。
    """
    deps, _ = _counting(engine, "tenant-a")
    broken = AgentDeps(
        store=deps.store,
        now=lambda: NOW,
        public_scripts=pd.DataFrame({"script_id": ["pub-nianlun"]}),
        script_aliases=deps.script_aliases,
    )
    result = _call(
        broken, {"action": "remember", "query": "年伦", "script_name": "年轮"}
    )
    assert result["error_code"] == "public_library_unavailable"
    assert _rows(engine) == 0


# ---------------------------------------------------------------------------
# 六、Excel 模式：JSON sidecar
# ---------------------------------------------------------------------------


def test_json_backend_roundtrips_through_a_new_instance(tmp_path):
    path = tmp_path / "script_aliases.json"
    JsonScriptAliasMemory(path).remember("青搂", "pub-qinglou", public_frame=PUBLIC)

    assert JsonScriptAliasMemory(path).lookup("青搂") == "pub-qinglou"
    assert json.loads(path.read_text(encoding="utf-8")) == {"青搂": "pub-qinglou"}


def test_json_backend_treats_missing_and_corrupt_files_as_empty(tmp_path):
    missing = JsonScriptAliasMemory(tmp_path / "nope.json")
    assert missing.lookup("青搂") is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert JsonScriptAliasMemory(corrupt).lookup("青搂") is None

    wrong_shape = tmp_path / "list.json"
    wrong_shape.write_text("[1, 2, 3]", encoding="utf-8")
    assert JsonScriptAliasMemory(wrong_shape).lookup("青搂") is None


def test_json_backend_raises_alias_unavailable_when_it_cannot_write(tmp_path):
    """写失败要能被告知，且不能把半截文件留在磁盘上。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    memory = JsonScriptAliasMemory(blocker / "script_aliases.json")

    with pytest.raises(AliasUnavailable):
        memory.remember("青搂", "pub-qinglou", public_frame=PUBLIC)
    assert memory.lookup("青搂") is None


def test_build_script_aliases_picks_backend_by_mode(tmp_path, engine):
    assert isinstance(
        build_script_aliases(uses_database=True, engine=engine),
        SqlScriptAliasMemory,
    )
    assert build_script_aliases(uses_database=True) is None
    json_backend = build_script_aliases(uses_database=False, data_dir=tmp_path)
    assert isinstance(json_backend, JsonScriptAliasMemory)
    assert json_backend.path == tmp_path / "script_aliases.json"


def test_first_write_wins_and_never_echoes_the_loser_target(engine):
    """同一条说法写第二次时，只回「已收录」，不回 ``script_id``。

    回显调用方想要的 ``script_id`` 会让模型以为「我记下的就是这个」，
    而实际存储可能是别处先写下的那条——这既是误导，也是在用回显掩盖
    真实状态。

    注意：**跨门店映射到不同剧本这条路已经走不通了**。一个说法若能同时
    对齐两个剧名，``ambiguous_query`` 会直接拒掉（先写者凭什么替所有人
    决定）；能通过校验的说法，在全库范围内只对齐一条。所以 ``created=False``
    只剩「同一个说法写了两次」这一种情形。
    """
    frame = pd.DataFrame(
        {
            "script_id": ["pub-dj", "pub-lc"],
            "script_name": ["金陵有座东君书院", "漓川怪谈簿"],
        }
    )
    first = SqlScriptAliasMemory(engine).remember(
        "东君书阮", "pub-dj", public_frame=frame
    )
    assert first["created"] is True
    assert first["script_id"] == "pub-dj"

    second = SqlScriptAliasMemory(engine).remember(
        "东君书阮", "pub-dj", public_frame=frame
    )
    assert second["created"] is False
    assert "script_id" not in second

    assert SqlScriptAliasMemory(engine).lookup("东君书阮") == "pub-dj"
    assert _rows(engine) == 1


# ---------------------------------------------------------------------------
# 七、真实公共库上的攻击样本（回归：旧的重合度规则全部放行）
# ---------------------------------------------------------------------------
#
# 旧规则是「bigram 重合度 >= 0.5 就接受」。下面每一条都在真实 5,809 行公共库
# 上实测被**接受**过——名字后面挂「晚场 / 半价」，或前面挂一整个客户名和地址。
# 现在的规则按长度差 + 编辑距离判断，逐条必须拒绝。
#
# 这一组存在的意义不是覆盖率，而是：上一版 31 个测试全绿，却漏掉了它们，
# 因为所有 PII 用例都恰好用了 2 字短名，被 `len(target) <= 3` 那条分支挡住。
# 短名的分支救了测试，没救长名。

LIBRARY = pd.DataFrame(
    {
        "script_id": ["pub-dj", "pub-gl", "pub-bjnh2"],
        "script_name": ["金陵有座东君书院", "格林格林", "病娇男孩的精分日记"],
    }
)


@pytest.mark.parametrize(
    ("query", "script_id"),
    [
        # 剧名 + 一小段无关中文
        ("东君书院晚场", "pub-dj"),
        ("东君书院半价", "pub-dj"),
        ("东君书院张三", "pub-dj"),
        # 客户名 / 地址 + 剧名
        ("张三要玩病娇男孩的精", "pub-bjnh2"),
        ("王小明住在杭州市西湖区练习时长两年半的魔法是用来", "pub-bjnh2"),
        # 重复字的名字：它们的不同字 bigram 集合很小，重合度天然被抬高
        ("格林童话", "pub-gl"),
        ("乌拉真好玩呀", "pub-gl"),
        ("哈哈哈真好玩", "pub-gl"),
        # 拆开写的手机号：旧规则只认连续 11 位
        ("138 0013 8000", "pub-dj"),
        ("138-0013-8000", "pub-dj"),
    ],
)
def test_arbitrary_text_can_never_be_stored(query, script_id):
    with pytest.raises(AliasRejected):
        prepare_alias(query, script_id, LIBRARY)


@pytest.mark.parametrize(
    ("query", "script_id"),
    [
        # 真正的错别字：长度不变，改一两个字
        ("东君书阮", "pub-dj"),
        ("金陵有座东君书阮", "pub-dj"),
        ("病娇男孩的精分曰记", "pub-bjnh2"),
        # 漏一个字也算打字误差（长度差 1）
        ("病娇男孩的精分记", "pub-bjnh2"),
    ],
)
def test_genuine_typos_are_still_learnable(query, script_id):
    """收紧规则不能收紧到「什么都学不了」——那就白做了。"""
    key, target = prepare_alias(query, script_id, LIBRARY)
    assert target == script_id
    assert key


# ---------------------------------------------------------------------------
# 八、第二轮的边界：看不见的字符、数字、短键、歧义、配额
# ---------------------------------------------------------------------------
#
# 上一版按「连续子串 + 编辑距离」判完就收工，下面每一条都用真实公共库
# 实测能写进去。它们合起来说明一件事：**同一条判据挡不住同一类攻击的
# 变体**，得按字符类别、数字、长度分段各补一条。


@pytest.mark.parametrize(
    "query",
    [
        "年轮\x9b",  # C1 控制符：[\x00-\x1f\x7f] 之外
        "年轮​",  # 零宽空格
        "年轮‮",  # RTL OVERRIDE：显示上能把「年轮」写成「轮年」
        "年轮\ud800",  # 孤立代理对：JSON 后端写盘时抛 UnicodeEncodeError
        "年\u0000轮",
        # 下面这些不是 C 类，大类检查整类放行，但屏幕上同样什么都不显示。
        # 不拦的后果是同一句真名能刷出无数条肉眼一致、码位不同的键。
        "年轮͏",  # COMBINING GRAPHEME JOINER（Mn）
        "年轮️",  # VARIATION SELECTOR-16（Mn）
        "年轮ㅤ",  # HANGUL FILLER（Lo）
        "年轮⠀",  # BRAILLE PATTERN BLANK（So）
        "年轮ᅟ",  # HANGUL CHOSEONG FILLER（Lo）
        "年轮ﾠ",  # HALFWIDTH HANGUL FILLER（Lo）
        # 同类的漏网码位：名字里就写着 FILLER / BLANK / REPLACEMENT。漏掉时
        # 同一句真名能插出 8 条肉眼一致、码位不同的键，把该剧本的说法配额
        # 占满，而合法错拼随后会被 alias_quota_exceeded 拒掉。
        "年轮￼",  # OBJECT REPLACEMENT CHARACTER（So）
        "年轮�",  # REPLACEMENT CHARACTER（So）
        "年轮᠆",  # MONGOLIAN TODO SOFT HYPHEN
        "年轮\U00016fe4",  # KHITAN SMALL SCRIPT FILLER（Mn）
        "年轮␢",  # BLANK SYMBOL
        "年轮꣹",  # DEVANAGARI GAP FILLER
        "年轮\U00011c44",  # BHAIKSUKI GAP FILLER-1
        "年轮\U00011f48",  # KAWI PUNCTUATION SPACE FILLER
        "年轮\U00013440",  # EGYPTIAN HIEROGLYPH MIRROR HORIZONTALLY（Mn）
        "年轮\U00013441",  # EGYPTIAN HIEROGLYPH FULL BLANK（Lo）
        "年轮\U00013442",  # EGYPTIAN HIEROGLYPH HALF BLANK（Lo）
        "年轮\U0001d159",  # MUSICAL SYMBOL NULL NOTEHEAD（So）
        # casefold 自己会造出组合符："İ".casefold() == "i" + U+0307。
        "年轮İ",
    ],
)
def test_invisible_characters_are_rejected(query):
    with pytest.raises(AliasRejected) as info:
        prepare_alias(query, "pub-nianlun", PUBLIC)
    assert info.value.code == "sensitive_query"


def test_visible_combining_marks_are_allowed_in_a_key():
    """组合用附加符是看得见的变音符号，不能和「零宽」混为一谈。

    一刀切拦 Mn 会把真的带变音符号的本名连同错拼一起误杀；这里只拦
    渲染为空的码位，外加 casefold 自己引入的组合符。
    """
    query = "Jose\u0301"  # 分解写法：e + COMBINING ACUTE ACCENT（Mn）
    frame = pd.DataFrame({"script_id": ["pub-jose"], "script_name": ["Jose"]})
    key, target = prepare_alias(query, "pub-jose", frame)
    assert (key, target) == ("jose\u0301", "pub-jose")


def test_trailing_whitespace_is_still_fine():
    """首尾空白不是攻击，别把粘贴带进来的换行也算成不可见字符。"""
    key, target = prepare_alias("  年伦\n", "pub-nianlun", PUBLIC)
    assert (key, target) == ("年伦", "pub-nianlun")


def test_number_markers_are_checked_as_numbers_not_just_ascii_digits():
    """①是 Unicode 的 No 类，``\\d`` 与 ``str.isdecimal`` 都不认它。

    断言错误码而不只是「抛了异常」：换成只测十进制数字的写法以后，
    「年轮①」会落到拼写检查上，报 ``not_similar``——照样抛异常，测试
    却什么都发现不了。
    """
    with pytest.raises(AliasRejected) as info:
        prepare_alias("年轮①", "pub-nianlun", PUBLIC)
    assert info.value.code == "sensitive_query"


NUMERIC = pd.DataFrame(
    {
        "script_id": ["pub-1440", "pub-816", "pub-4149", "pub-nianlun"],
        "script_name": ["1440", "8/16", "4149", "年轮"],
    }
)


@pytest.mark.parametrize("query", ["16号", "16点", "16人", "16元"])
def test_price_and_schedule_shaped_keys_do_not_ride_on_a_numeric_title(query):
    """《8/16》的数字串是 816，键里的 16 对不上，就不该当成它的说法。"""
    with pytest.raises(AliasRejected):
        prepare_alias(query, "pub-816", NUMERIC)


@pytest.mark.parametrize(
    ("query", "script_id"),
    [
        # 数字串一模一样，只有货币符号 / 单位 / 时间分隔符不同——只比数字
        # 的写法会把它们全当成「另一种拼写」永久写进全局表。
        ("￥1440", "pub-1440"),
        ("$1440", "pub-1440"),
        ("1440块", "pub-1440"),
        ("14:40", "pub-1440"),
        ("14点40", "pub-1440"),
        ("8月16", "pub-816"),
        ("8/16点", "pub-816"),
        ("8-16", "pub-816"),
        ("4149元", "pub-4149"),
    ],
)
def test_units_and_separators_do_not_ride_on_a_numeric_title(query, script_id):
    """数字对得上还不够，数字之外的骨架也要对得上。

    真实公共库里有《1440》《8/16》《4149》这类纯数字剧名。店长说「￥1440
    那场」「14:40 的场」时，数字串仍然是 1440 / 816，而允许一字差异的对齐
    规则会放行——报价和场次时间就是这么进共享表的。
    """
    with pytest.raises(AliasRejected) as info:
        prepare_alias(query, script_id, NUMERIC)
    assert info.value.code == "not_similar"


def test_the_same_separator_spelled_differently_is_still_learnable():
    """骨架里带汉字时不能收紧到逐字相同：全角 / 半角这类写法要能进来。"""
    frame = pd.DataFrame(
        {
            "script_id": ["pub-10", "pub-nianlun"],
            "script_name": ["10%无罪解答", "年轮"],
        }
    )
    key, target = prepare_alias("10％无罪解答", "pub-10", frame)
    assert (key, target) == ("10％无罪解答", "pub-10")


def test_typo_on_the_chinese_part_of_a_numeric_title_is_still_learnable():
    frame = pd.DataFrame(
        {
            "script_id": ["pub-ql3", "pub-nianlun"],
            "script_name": ["青楼3月下美人", "年轮"],
        }
    )
    key, target = prepare_alias("青楼3月下羙人", "pub-ql3", frame)
    assert (key, target) == ("青楼3月下羙人", "pub-ql3")


@pytest.mark.parametrize("query", ["好的吧", "老板说", "完了吗", "张三来"])
def test_three_character_conversation_text_cannot_hang_off_a_long_title(query):
    """3 字键只允许是原名里逐字连续的一段。

    允许它带一个错字，等于「三个字里有两个字对上就能存」——而两字窗口在
    5,809 个剧名里几乎总能找到，日常对话就全都能写进全局表。
    """
    with pytest.raises(AliasRejected):
        prepare_alias(query, "pub-dj", LIBRARY)


def test_three_character_abbreviation_that_really_is_a_prefix_is_fine():
    """真正的简称（逐字出现在标题里）不受上面那条影响。"""
    key, target = prepare_alias("东君书", "pub-dj", LIBRARY)
    assert target == "pub-dj"


def test_a_phrase_that_fits_several_titles_is_not_recorded():
    """「差一个字就是别的剧名」和「已经是别的剧名」一样不能记。

    系列本（《东京恐怖故事：加藤病院》/《如月车站》）下，共同的短前缀同时
    像所有分册；先确认它的门店会把所有人永久钉在其中一条上。
    """
    frame = pd.DataFrame(
        {
            "script_id": ["pub-a", "pub-b"],
            "script_name": ["东京恐怖故事：加藤病院", "东京恐怖故事：如月车站"],
        }
    )
    with pytest.raises(AliasRejected) as info:
        prepare_alias("东京恐怖故是", "pub-a", frame)
    assert info.value.code == "ambiguous_query"


def test_a_full_title_typo_inside_a_series_is_still_learnable():
    """带分册后半截的全名是唯一的一条，歧义规则不该连它一起拒。"""
    frame = pd.DataFrame(
        {
            "script_id": ["pub-a", "pub-b"],
            "script_name": ["东京恐怖故事：加藤病院", "东京恐怖故事：如月车站"],
        }
    )
    assert prepare_alias("东京恐怖故是：加藤病院", "pub-a", frame) == (
        "东京恐怖故是：加藤病院",
        "pub-a",
    )


def test_ambiguous_is_not_triggered_by_an_unrelated_second_title():
    """歧义判定不能退化成「库里有别的本就不让学」。"""
    frame = pd.DataFrame(
        {
            "script_id": ["pub-a", "pub-b"],
            "script_name": ["金陵有座东君书院", "漓川怪谈簿"],
        }
    )
    assert prepare_alias("东君书阮", "pub-a", frame) == ("东君书阮", "pub-a")


def test_two_character_keys_are_exempt_from_the_ambiguity_rule():
    """两字说法不做歧义判定，这是有意的。

    真实库里有 7 个「青X」两字本，任何错字都同时像全部 7 个，判定会把两字
    名的错拼全拒掉——而两字查询正是 bigram 兜底明确不覆盖的一段，全局记忆
    是它唯一的救兵。代价是「拼场」这类日常词也可能被确认进表，影响范围
    只有一条两字键，读出来还会带「来自其他门店」的提示。
    """
    frame = pd.DataFrame(
        {
            "script_id": ["pub-a", "pub-b", "pub-c"],
            "script_name": ["青楼", "青沙", "青史"],
        }
    )
    assert prepare_alias("青搂", "pub-a", frame) == ("青搂", "pub-a")


def test_pandas_na_target_is_a_controlled_rejection():
    """模型写完 JSON 后 script_id 常常是 None / pd.NA，不能变成 TypeError。"""
    with pytest.raises(AliasRejected) as info:
        prepare_alias("年伦", pd.NA, PUBLIC)
    assert info.value.code == "bad_request"


def test_corrupt_sidecar_is_not_silently_overwritten(tmp_path):
    """读不出来就当空的，然后整表重写——那等于悄悄清空所有记忆。

    读路径可以降级（搜索照常），写路径必须失败。
    """
    path = tmp_path / "script_aliases.json"
    path.write_text("{not json", encoding="utf-8")
    memory = JsonScriptAliasMemory(path)

    assert memory.lookup("青搂") is None
    with pytest.raises(AliasUnavailable):
        memory.remember("青搂", "pub-qinglou", public_frame=PUBLIC)
    assert path.read_text(encoding="utf-8") == "{not json"


def test_quota_stops_the_global_table_from_growing_without_bound(
    engine, monkeypatch
):
    """一行一个字的攒法单看每行都合规，只能靠总量兜住。"""
    from app.store import script_aliases as module

    frame = pd.DataFrame(
        {
            "script_id": ["pub-dj", "pub-lc", "pub-bjnh2"],
            "script_name": ["金陵有座东君书院", "漓川怪谈簿", "病娇男孩的精分日记"],
        }
    )
    monkeypatch.setattr(module, "_MAX_ROWS_TOTAL", 1)
    memory = SqlScriptAliasMemory(engine)
    memory.remember("东君书阮", "pub-dj", public_frame=frame)

    with pytest.raises(AliasRejected) as info:
        memory.remember("漓川怪谈薄", "pub-lc", public_frame=frame)
    assert info.value.code == "alias_quota_exceeded"
    assert _rows(engine) == 1


def test_quota_does_not_block_a_key_that_is_already_recorded(engine, monkeypatch):
    """重复确认同一个说法要回「已收录」，不能回「记忆已满」。"""
    from app.store import script_aliases as module

    frame = pd.DataFrame(
        {
            "script_id": ["pub-dj", "pub-lc"],
            "script_name": ["金陵有座东君书院", "漓川怪谈簿"],
        }
    )
    monkeypatch.setattr(module, "_MAX_ROWS_TOTAL", 1)
    memory = SqlScriptAliasMemory(engine)
    memory.remember("东君书阮", "pub-dj", public_frame=frame)

    again = memory.remember("东君书阮", "pub-dj", public_frame=frame)
    assert again == {"query_key": "东君书阮", "created": False}


def test_json_quota_uses_the_same_limits(tmp_path, monkeypatch):
    from app.store import script_aliases as module

    frame = pd.DataFrame(
        {
            "script_id": ["pub-dj", "pub-lc"],
            "script_name": ["金陵有座东君书院", "漓川怪谈簿"],
        }
    )
    monkeypatch.setattr(module, "_MAX_ROWS_TOTAL", 1)
    memory = JsonScriptAliasMemory(tmp_path / "script_aliases.json")
    memory.remember("东君书阮", "pub-dj", public_frame=frame)

    with pytest.raises(AliasRejected) as info:
        memory.remember("漓川怪谈薄", "pub-lc", public_frame=frame)
    assert info.value.code == "alias_quota_exceeded"
    assert json.loads((tmp_path / "script_aliases.json").read_text("utf-8")) == {
        "东君书阮": "pub-dj"
    }


def test_per_target_quota_caps_how_many_spellings_one_title_can_have(
    engine, monkeypatch
):
    """总量上限管不住「一个本被反复确认」：单个剧本另外封顶。

    两句不同的错拼各自都合规，加起来却足够把某个本的所有查询淹没。
    """
    from app.store import script_aliases as module

    frame = pd.DataFrame(
        {
            "script_id": ["pub-dj", "pub-lc"],
            "script_name": ["金陵有座东君书院", "漓川怪谈簿"],
        }
    )
    monkeypatch.setattr(module, "_MAX_ROWS_PER_TARGET", 1)
    memory = SqlScriptAliasMemory(engine)
    memory.remember("东君书阮", "pub-dj", public_frame=frame)

    with pytest.raises(AliasRejected) as info:
        memory.remember("东君书院", "pub-dj", public_frame=frame)
    assert info.value.code == "alias_quota_exceeded"
    assert _rows(engine) == 1


# 用独立进程跑一次真实写入：sidecar 的锁必须跨进程生效，线程测不出来。
_CHILD_REMEMBER = """
import json
import sys

root, sidecar, name, query, script_id, started, marker = sys.argv[1:8]
sys.path.insert(0, root)

import pandas as pd

from app.store.script_aliases import JsonScriptAliasMemory

frame = pd.DataFrame({"script_id": [script_id], "script_name": [name]})
memory = JsonScriptAliasMemory(sidecar)
with open(started, "w", encoding="utf-8"):
    pass
payload = memory.remember(query, script_id, public_frame=frame)
with open(marker, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False)
"""


def _start_child(tmp_path, sidecar, name, query, script_id, tag):
    """起一个子进程去写同一个 sidecar；返回 (进程, 已启动标记, 完成标记)。"""
    script = tmp_path / "child_remember.py"
    script.write_text(_CHILD_REMEMBER, encoding="utf-8")
    started = tmp_path / f"{tag}.started"
    marker = tmp_path / f"{tag}.done"
    process = subprocess.Popen(
        [
            sys.executable,
            str(script),
            str(Path(__file__).resolve().parents[1]),
            str(sidecar),
            name,
            query,
            script_id,
            str(started),
            str(marker),
        ],
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    return process, started, marker


def _wait_for(path: Path, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"子进程没有写出 {path.name}")


def test_a_write_in_another_process_waits_for_the_sidecar_lock(tmp_path):
    """网页进程和 CLI 会同时打开同一个 ``data/script_aliases.json``。

    进程内的 RLock 保护不了对方；没有跨进程锁时，两边都读到旧表，后写的
    那份把先写的键静默丢掉，而两个调用方都回「已记录」。
    """
    from app.store.script_aliases import _sidecar_guard

    path = tmp_path / "script_aliases.json"
    process = None
    try:
        with _sidecar_guard(path):
            process, started, marker = _start_child(
                tmp_path, path, "青楼", "青搂", "pub-q", "slow"
            )
            _wait_for(started)
            time.sleep(0.6)  # 让它走到拿锁那一步
            assert not marker.exists(), "另一进程越过锁写进了 sidecar"
        assert process.wait(timeout=120) == 0
        assert json.loads(marker.read_text("utf-8"))["created"] is True
        assert json.loads(path.read_text("utf-8")) == {"青搂": "pub-q"}
    finally:
        if process is not None and process.poll() is None:
            process.kill()


def test_two_processes_writing_at_once_both_keep_their_key(tmp_path):
    """两个进程同时写不同的键，两行都要在——这是整文件替换最怕的场合。"""
    path = tmp_path / "script_aliases.json"
    first = _start_child(tmp_path, path, "青楼", "青搂", "pub-q", "a")
    second = _start_child(
        tmp_path, path, "漓川怪谈簿", "漓川怪谈薄", "pub-l", "b"
    )
    try:
        assert first[0].wait(timeout=120) == 0
        assert second[0].wait(timeout=120) == 0
    finally:
        for process, _, _ in (first, second):
            if process.poll() is None:
                process.kill()
    stored = json.loads(path.read_text("utf-8"))
    assert stored == {"青搂": "pub-q", "漓川怪谈薄": "pub-l"}


def test_two_instances_on_one_sidecar_do_not_clobber_each_other(tmp_path):
    """同一个文件被开两个实例（网页进程 + CLI）时，锁要按路径共享。

    实例锁保护不了对方，旧实现还会用同一个固定临时文件名互相覆盖。
    """
    path = tmp_path / "script_aliases.json"
    left = JsonScriptAliasMemory(path)
    right = JsonScriptAliasMemory(path)

    barrier = threading.Barrier(2)

    def writer(memory, query):
        barrier.wait()
        memory.remember(query, "pub-qinglou", public_frame=PUBLIC)

    threads = [
        threading.Thread(target=writer, args=(left, "青搂")),
        threading.Thread(target=writer, args=(right, "青娄")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert set(stored) == {"青搂", "青娄"}


def test_search_tool_reports_a_learned_hit_end_to_end(engine):
    """走一遍工具层：确认过的说法在下一次 search 里就是 learned 命中。

    上面几条测的是 ``find_scripts`` 与 store，这条测 Agent 工具真的把它
    接上了——否则「接了一半」也会全绿。
    """
    deps, _ = _counting(engine, "tenant-a")
    assert _call(
        deps, {"action": "remember", "query": "年伦", "script_id": "pub-nianlun"}
    )["ok"] is True

    result = _call(deps, {"action": "search", "query": "年伦"})
    assert result["ok"] is True
    assert [item["match_quality"] for item in result["data"]["candidates"]] == [
        "learned"
    ]
    assert any("其他门店" in warning for warning in result["warnings"])


def test_get_tool_labels_suggestions_instead_of_returning_bare_records(engine):
    """``get`` 未命中时返回的是候选，不能给一串看着像已确认记录的档案。"""
    deps, _ = _counting(engine, "tenant-a")
    result = _call(deps, {"action": "get", "query": "病娇男孩的精分曰记"})
    assert result["error_code"] == "script_not_found"
    suggestions = result["data"]["suggestions"]
    assert [item["match_quality"] for item in suggestions] == ["suggested"]
    assert any("确认" in warning for warning in result["warnings"])


# ---------------------------------------------------------------------------
# 九、渲染为空的填充码位不能把说法配额刷满
# ---------------------------------------------------------------------------


def test_blank_fillers_cannot_burn_a_titles_spelling_quota(tmp_path):
    """同一句真名插满各种空白码位，也只能得到这一条映射。

    这是 ``_has_invisible`` 漏码位的实际后果：8 条配额被永远搜不到的键占满，
    店长随后确认的那个真错拼反而被 ``alias_quota_exceeded`` 拒掉。码位每漏
    一个，这里就多一条能写进去的废键。
    """
    frame = pd.DataFrame(
        {"script_id": ["pub-dj"], "script_name": ["金陵有座东君书院"]}
    )
    memory = JsonScriptAliasMemory(tmp_path / "script_aliases.json")
    fillers = [
        "\ufffc",  # OBJECT REPLACEMENT CHARACTER
        "\ufffd",  # REPLACEMENT CHARACTER
        "\u1806",  # MONGOLIAN TODO SOFT HYPHEN
        "\u2422",  # BLANK SYMBOL
        "\ua8f9",  # DEVANAGARI GAP FILLER
        "\U00011c44",  # BHAIKSUKI GAP FILLER-1
        "\U00011f48",  # KAWI PUNCTUATION SPACE FILLER
        "\U00013441",  # EGYPTIAN HIEROGLYPH FULL BLANK
        "\U00013442",  # EGYPTIAN HIEROGLYPH HALF BLANK
        "\U00016fe4",  # KHITAN SMALL SCRIPT FILLER
        "\U0001d159",  # MUSICAL SYMBOL NULL NOTEHEAD
    ]
    for filler in fillers:
        with pytest.raises(AliasRejected) as info:
            memory.remember(
                f"金陵有座{filler}东君书院", "pub-dj", public_frame=frame
            )
        assert info.value.code == "sensitive_query", hex(ord(filler))
    # 真错拼仍写得进去，说明配额一个都没被占。
    payload = memory.remember("东君书阮", "pub-dj", public_frame=frame)
    assert payload["created"] is True
    assert json.loads(
        (tmp_path / "script_aliases.json").read_text("utf-8")
    ) == {"东君书阮": "pub-dj"}


# ---------------------------------------------------------------------------
# 十、悬空映射：旧目标已不在公共库时，重新指向而不是吞掉这次确认
# ---------------------------------------------------------------------------

STALE_FRAME = pd.DataFrame(
    {"script_id": ["pub-yue"], "script_name": ["东京恐怖故事：如月车站"]}
)
STALE_KEY = "东京恐怖故是"
STALE_TARGET = "pub-gone"


def test_a_stale_mapping_is_repointed_instead_of_swallowed(engine):
    """公共库整表重导后，旧 id 会消失；那条映射读出来等于没学到。

    但它还占着键和一行配额。旧实现只看「键在不在」，于是店长这次合法的
    确认被回成「此前已有记录」，而磁盘上仍然指向一个不存在的本——确认
    被静默丢弃，且再也改不了。
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO script_aliases"
                "(query_key, script_id, created_at, updated_at) "
                "VALUES (:key, :sid, '2026-01-01 00:00:00', "
                "'2026-01-01 00:00:00')"
            ),
            {"key": STALE_KEY, "sid": STALE_TARGET},
        )
    memory = SqlScriptAliasMemory(engine)
    assert memory.lookup(STALE_KEY) == STALE_TARGET

    payload = memory.remember(STALE_KEY, "pub-yue", public_frame=STALE_FRAME)

    assert payload["created"] is True
    assert payload["script_id"] == "pub-yue"
    assert memory.lookup(STALE_KEY) == "pub-yue"
    # 是改写不是新增：总行数没有涨。
    assert _rows(engine) == 1


def test_a_live_mapping_still_wins_over_a_later_confirmation(engine):
    """旧目标还在库里时保持「先写者胜」，不能因为来的是另一次确认就改写。"""
    memory = SqlScriptAliasMemory(engine)
    memory.remember(STALE_KEY, "pub-yue", public_frame=STALE_FRAME)

    payload = memory.remember(STALE_KEY, "pub-yue", public_frame=STALE_FRAME)

    assert payload == {"query_key": STALE_KEY, "created": False}
    assert memory.lookup(STALE_KEY) == "pub-yue"


def test_json_backend_repoints_a_stale_mapping_too(tmp_path):
    """Excel 模式走同一套语义：sidecar 里悬空的那条要能被改写。"""
    path = tmp_path / "script_aliases.json"
    path.write_text(
        json.dumps({STALE_KEY: STALE_TARGET}, ensure_ascii=False),
        encoding="utf-8",
    )
    memory = JsonScriptAliasMemory(path)

    payload = memory.remember(STALE_KEY, "pub-yue", public_frame=STALE_FRAME)

    assert payload["created"] is True
    assert json.loads(path.read_text("utf-8")) == {STALE_KEY: "pub-yue"}


def test_a_stale_row_still_counts_toward_the_target_quota(tmp_path, monkeypatch):
    """改写悬空键也要经过那个剧本的说法上限，不能不限次往里塞。"""
    from app.store import script_aliases as module

    monkeypatch.setattr(module, "_MAX_ROWS_PER_TARGET", 0)
    path = tmp_path / "script_aliases.json"
    path.write_text(json.dumps({STALE_KEY: STALE_TARGET}), encoding="utf-8")
    memory = JsonScriptAliasMemory(path)

    with pytest.raises(AliasRejected) as info:
        memory.remember(STALE_KEY, "pub-yue", public_frame=STALE_FRAME)
    assert info.value.code == "alias_quota_exceeded"
    # 没有因为它被拒就把原有内容改坏了。
    assert json.loads(path.read_text("utf-8")) == {STALE_KEY: STALE_TARGET}


# ---------------------------------------------------------------------------
# 十一、读也参与跨进程锁（Windows 的 os.replace 怕被打开的句柄）
# ---------------------------------------------------------------------------

_CHILD_HOLD = """
import sys
import time
from pathlib import Path

root, sidecar, hold, started, done = sys.argv[1:6]
sys.path.insert(0, root)

from app.store.script_aliases import _sidecar_guard

with _sidecar_guard(Path(sidecar)):
    Path(started).write_text("", encoding="utf-8")
    time.sleep(float(hold))
Path(done).write_text("", encoding="utf-8")
"""


def _start_holder(tmp_path, sidecar, hold):
    script = tmp_path / "child_hold.py"
    script.write_text(_CHILD_HOLD, encoding="utf-8")
    started = tmp_path / "hold.started"
    done = tmp_path / "hold.done"
    process = subprocess.Popen(
        [
            sys.executable,
            str(script),
            str(Path(__file__).resolve().parents[1]),
            str(sidecar),
            str(hold),
            str(started),
            str(done),
        ],
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    return process, started, done


def test_a_reader_waits_for_the_sidecar_lock_too(tmp_path):
    """读也要排队。

    Windows 上 ``os.replace`` 需要目标文件没被任何句柄打开（CPython 的
    ``open`` 不带 ``FILE_SHARE_DELETE``），一次读正好撞上替换，写侧就
    ``PermissionError``、这次确认整条落空（实测 400 次写里约 80 次）。
    所以 ``lookup`` 也必须进同一把锁——它一旦绕过锁，写侧的失败就回来了。
    """
    path = tmp_path / "script_aliases.json"
    path.write_text("{}", encoding="utf-8")
    memory = JsonScriptAliasMemory(path)
    process, started, _ = _start_holder(tmp_path, path, 1.5)
    try:
        _wait_for(started)
        began = time.monotonic()
        assert memory.lookup("青搂") is None
        elapsed = time.monotonic() - began
        assert elapsed >= 0.4, f"lookup 没有等锁（{elapsed:.2f}s）"
    finally:
        process.kill()
        process.wait(timeout=60)


def test_lookup_still_returns_none_when_the_lock_cannot_be_taken(tmp_path):
    """锁拿不到时读要降级成「没学到」，不能把搜索弄成异常。"""

    def explode(handle):
        raise OSError("lock unavailable")

    from app.store import script_aliases as module

    path = tmp_path / "script_aliases.json"
    path.write_text(json.dumps({"青搂": "pub-q"}), encoding="utf-8")
    memory = JsonScriptAliasMemory(path)
    original = module._lock_file
    module._lock_file = explode
    try:
        assert memory.lookup("青搂") is None
    finally:
        module._lock_file = original
