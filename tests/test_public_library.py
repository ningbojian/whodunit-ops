"""公共剧本库：coerce/合并/遮蔽/种子/导入 CLI/采集格式容错。"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from app.store.import_public_scripts import (
    main as import_main,
    normalize_rows,
    parse_duration_text,
    parse_seats_text,
)
from app.tools.public_library import (
    PUBLIC_COLUMNS,
    apply_merge,
    coerce_public_scripts,
    empty_public_scripts,
    ensure_public_seed,
    is_public_script_id,
    load_public_scripts,
    make_public_script_id,
    merge_public_scripts,
    merge_with_shadow,
    reset_seed_guard,
)
from app.tools.scripts import coerce_scripts, make_script_id
from app.tools.storage import is_missing_value

NOW = pd.Timestamp("2026-10-22 12:00:00")


def _public_row(**overrides):
    row = {
        "script_id": make_public_script_id("九霄"),
        "script_name": "九霄",
        "aliases": "",
        "script_type": "情感",
        "total_seats": 7,
        "duration_minutes": 270,
        "list_price": None,
        "notes": "",
        "source_text": "",
        "created_at": NOW,
        "updated_at": NOW,
        "origin": "策展",
        "imported_at": NOW,
    }
    row.update(overrides)
    return row


def _private_row(**overrides):
    row = {
        "script_id": make_script_id("雾隐山庄"),
        "script_name": "雾隐山庄",
        "aliases": "",
        "script_type": "推理",
        "total_seats": 6,
        "duration_minutes": 240,
        "list_price": 168,
        "notes": "",
        "source_text": "",
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


class TestPublicIds:
    def test_pub_prefix_roundtrip(self):
        pid = make_public_script_id("九霄")
        assert pid == "pub-" + make_script_id("九霄")
        assert is_public_script_id(pid)
        assert not is_public_script_id(make_script_id("九霄"))
        assert not is_public_script_id(None)
        assert not is_public_script_id("")

    def test_coerce_adds_origin_columns(self):
        df = coerce_public_scripts(pd.DataFrame([_private_row()]))
        assert list(df.columns) == PUBLIC_COLUMNS
        assert (df["origin"].fillna("") == "").all()


class TestApplyMerge:
    def test_add_update_skip(self):
        existing = empty_public_scripts()
        incoming = coerce_public_scripts(pd.DataFrame([_public_row()]))
        merged, stats, errors = apply_merge(existing, incoming, now=NOW)
        assert stats == {"added": 1, "updated": 0, "skipped": 0}
        assert not errors
        assert len(merged) == 1

        # 同名无变化 → skipped
        merged2, stats2, _ = apply_merge(merged, incoming, now=NOW)
        assert stats2 == {"added": 0, "updated": 0, "skipped": 1}
        assert len(merged2) == 1

        # 空值不覆盖：只补空的 list_price
        filled = coerce_public_scripts(pd.DataFrame([_public_row(list_price=288)]))
        merged3, stats3, _ = apply_merge(merged2, filled, now=NOW)
        assert stats3["updated"] == 1
        assert int(merged3.iloc[0]["list_price"]) == 288

        # 非空也不覆盖已有值（公共库合并是补全不是覆写）
        overwrite = coerce_public_scripts(
            pd.DataFrame([_public_row(duration_minutes=300, list_price=1)])
        )
        merged4, stats4, _ = apply_merge(merged3, overwrite, now=NOW)
        assert int(merged4.iloc[0]["duration_minutes"]) == 270
        assert int(merged4.iloc[0]["list_price"]) == 288

    def test_missing_name_or_origin_goes_to_errors(self):
        incoming = coerce_public_scripts(
            pd.DataFrame(
                [
                    _public_row(script_name="", origin="策展"),
                    _public_row(script_name="无来源本", origin=""),
                    _public_row(),
                ]
            )
        )
        merged, stats, errors = apply_merge(empty_public_scripts(), incoming, now=NOW)
        assert stats["added"] == 1
        assert len(errors) == 2
        assert len(merged) == 1

    def test_origin_join_on_change(self):
        existing = coerce_public_scripts(pd.DataFrame([_public_row(origin="策展")]))
        incoming = coerce_public_scripts(
            pd.DataFrame([_public_row(origin="crawler:vshang66:http://x/1.html")])
        )
        merged, stats, _ = apply_merge(existing, incoming, now=NOW)
        assert stats["updated"] == 1
        origin = str(merged.iloc[0]["origin"])
        assert "策展" in origin and "crawler:vshang66" in origin and "; " in origin

        # 重复导入相同 origin 不再变化
        _, stats2, _ = apply_merge(merged, incoming, now=NOW)
        assert stats2["skipped"] == 1


class TestShadow:
    def test_private_wins_same_name(self):
        private = coerce_scripts(pd.DataFrame([_private_row()]))
        public = coerce_public_scripts(
            pd.DataFrame([_public_row(script_name="雾隐山庄", script_type="情感")])
        )
        out = merge_with_shadow(private, public)
        assert len(out) == 1
        assert str(out.iloc[0]["script_type"]) == "推理"
        assert not is_public_script_id(out.iloc[0]["script_id"])

    def test_public_rows_kept_with_pub_id(self):
        private = coerce_scripts(pd.DataFrame([_private_row()]))
        public = coerce_public_scripts(pd.DataFrame([_public_row()]))
        out = merge_with_shadow(private, public)
        assert len(out) == 2
        public_rows = [r for _, r in out.iterrows() if is_public_script_id(r["script_id"])]
        assert len(public_rows) == 1
        assert list(out.columns) == list(private.columns)

    def test_none_or_empty_public(self):
        private = coerce_scripts(pd.DataFrame([_private_row()]))
        assert len(merge_with_shadow(private, None)) == 1
        assert len(merge_with_shadow(private, empty_public_scripts())) == 1


class TestExcelBackend:
    def test_atomic_write_and_reload(self, tmp_path):
        path = tmp_path / "public.xlsx"
        stats, errors = merge_public_scripts(
            coerce_public_scripts(pd.DataFrame([_public_row()])), path=path, now=NOW
        )
        assert stats["added"] == 1 and not errors
        loaded = load_public_scripts(path)
        assert len(loaded) == 1
        assert str(loaded.iloc[0]["script_name"]) == "九霄"
        # 无残留 tmp 文件
        assert not list(tmp_path.glob("*.tmp"))

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_public_scripts(tmp_path / "nope.xlsx").empty

    def test_idempotent_second_merge(self, tmp_path):
        path = tmp_path / "public.xlsx"
        frame = coerce_public_scripts(pd.DataFrame([_public_row()]))
        merge_public_scripts(frame, path=path, now=NOW)
        stats2, _ = merge_public_scripts(frame, path=path, now=NOW)
        assert stats2 == {"added": 0, "updated": 0, "skipped": 1}


class TestSeed:
    @pytest.fixture(autouse=True)
    def _reset_guard(self):
        reset_seed_guard()
        yield
        reset_seed_guard()

    def test_seed_once_then_noop(self, tmp_path):
        path = tmp_path / "public.xlsx"
        seed = tmp_path / "seed.csv"
        seed.write_text(
            "script_name,script_type,origin\n九霄,情感,策展\n", encoding="utf-8"
        )
        result = ensure_public_seed(path=path, seed_path=seed, now=NOW)
        assert result["seeded"] is True and result["added"] == 1

        # 守卫 + 非空：二次 ensure 为 no-op
        result2 = ensure_public_seed(path=path, seed_path=seed, now=NOW)
        assert result2["seeded"] is False
        assert len(load_public_scripts(path)) == 1

    def test_cleared_library_can_reseed(self, tmp_path):
        path = tmp_path / "public.xlsx"
        seed = tmp_path / "seed.csv"
        seed.write_text(
            "script_name,script_type,origin\n九霄,情感,策展\n", encoding="utf-8"
        )
        ensure_public_seed(path=path, seed_path=seed, now=NOW)
        # 清库后重灌是期望行为（守卫就是库本身的空/非空）
        pd.DataFrame(columns=PUBLIC_COLUMNS).to_excel(path, index=False)
        reset_seed_guard()
        result = ensure_public_seed(path=path, seed_path=seed, now=NOW)
        assert result["seeded"] is True and result["added"] == 1

    def test_missing_seed_file_noop(self, tmp_path):
        result = ensure_public_seed(
            path=tmp_path / "public.xlsx", seed_path=tmp_path / "nope.csv", now=NOW
        )
        assert result["seeded"] is False
        assert result["reason"] == "seed_file_missing"

    def test_real_seed_csv_loads(self, tmp_path):
        """仓库内策展种子能灌入且不含演示剧本名（零重名）。"""
        path = tmp_path / "public.xlsx"
        result = ensure_public_seed(path=path, now=NOW)
        assert result["seeded"] is True
        loaded = load_public_scripts(path)
        assert len(loaded) >= 8
        names = set(loaded["script_name"].astype(str))
        assert not names & {"雾隐山庄", "长夜将明", "第七码头", "山鬼"}


class TestVshang66Parsers:
    """采集格式容错：区间时长取中值、人数/性别比拆分。"""

    def test_duration_range_midpoint_hours(self):
        minutes, note = parse_duration_text("7-8小时")
        assert minutes == 450  # 区间中值 7.5h
        assert "7-8小时" in note

    def test_duration_single_hours(self):
        minutes, _ = parse_duration_text("7小时")
        assert minutes == 420

    def test_duration_minutes_unit(self):
        minutes, _ = parse_duration_text("240分钟")
        assert minutes == 240

    def test_duration_unparseable(self):
        minutes, note = parse_duration_text("看情况")
        assert minutes is None and note

    def test_seats_with_gender_split(self):
        seats, note = parse_seats_text("6人（2男4女）")
        assert seats == 6
        assert "2男4女" in note

    def test_seats_plain(self):
        seats, note = parse_seats_text("8人本")
        assert seats == 8 and note == ""

    def test_normalize_vshang_like_rows(self):
        rows = [
            {
                "script_name": "倾丘",
                "script_type": "情感",
                "duration": "7-8小时",
                "seats": "6人（3男3女）",
                "notes": "标签：古风/架空/情感/变格；作者：惊鸿工作室",
                "collected_at": "2026-10-19",
                "origin": "crawler:vshang66:http://www.vshang66.com/6p/70407.html",
            }
        ]
        frame, errors = normalize_rows(rows)
        assert not errors
        row = frame.iloc[0]
        assert str(row["script_name"]) == "倾丘"
        assert int(row["duration_minutes"]) == 450
        assert int(row["total_seats"]) == 6
        assert "收录时间：2026-10-19" in str(row["notes"])
        assert "3男3女" in str(row["notes"])


class TestImportCli:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch, tmp_path):
        # 本地测试绝不连数据库：清 DATABASE_URL、显式 excel 后端。
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("WEB_STORAGE_BACKEND", "excel")

    def _write_json(self, tmp_path, rows):
        path = tmp_path / "in.json"
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return path

    def test_json_import_and_idempotent(self, tmp_path, capsys):
        rows = [
            {
                "script_name": "倾丘",
                "script_type": "情感",
                "duration": "7-8小时",
                "seats": "6人（3男3女）",
                "notes": "标签：古风/情感",
                "collected_at": "2026-10-19",
                "origin": "crawler:vshang66:http://www.vshang66.com/6p/70407.html",
            }
        ]
        path = self._write_json(tmp_path, rows)
        data_dir = tmp_path / "data"
        rc = import_main([str(path), "--data-dir", str(data_dir)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "新增 1" in out

        loaded = load_public_scripts(data_dir / "public_scripts.xlsx")
        assert len(loaded) == 1
        assert int(loaded.iloc[0]["duration_minutes"]) == 450
        assert int(loaded.iloc[0]["total_seats"]) == 6

        # 幂等：二次导入 added=0
        rc2 = import_main([str(path), "--data-dir", str(data_dir)])
        assert rc2 == 0
        out2 = capsys.readouterr().out
        assert "新增 0" in out2
        assert len(load_public_scripts(data_dir / "public_scripts.xlsx")) == 1

    def test_dry_run_does_not_write(self, tmp_path, capsys):
        rows = [{"script_name": "倾丘", "origin": "crawler:vshang66:http://x/1.html"}]
        path = self._write_json(tmp_path, rows)
        data_dir = tmp_path / "data"
        rc = import_main([str(path), "--dry-run", "--data-dir", str(data_dir)])
        assert rc == 0
        assert "未写库" in capsys.readouterr().out
        assert not (data_dir / "public_scripts.xlsx").exists()

    def test_missing_origin_rejected(self, tmp_path, capsys):
        rows = [
            {"script_name": "无来源本", "duration": "4小时"},
            {"script_name": "有来源本", "origin": "策展", "duration": "4小时"},
        ]
        path = self._write_json(tmp_path, rows)
        data_dir = tmp_path / "data"
        rc = import_main([str(path), "--data-dir", str(data_dir)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "缺 origin" in out
        loaded = load_public_scripts(data_dir / "public_scripts.xlsx")
        assert list(loaded["script_name"].astype(str)) == ["有来源本"]

    def test_csv_import_with_chinese_columns(self, tmp_path):
        path = tmp_path / "in.csv"
        path.write_text(
            "剧本名,类型,时长,角色,来源\n云使,推理,4.5小时,6人（3男3女）,策展\n",
            encoding="utf-8",
        )
        data_dir = tmp_path / "data"
        rc = import_main([str(path), "--data-dir", str(data_dir)])
        assert rc == 0
        loaded = load_public_scripts(data_dir / "public_scripts.xlsx")
        assert len(loaded) == 1
        assert int(loaded.iloc[0]["duration_minutes"]) == 270

    def test_seed_csv_dry_run(self, capsys):
        """验证命令行里的种子 dry-run 能跑通（不写真实 data/）。"""
        from app.config import PUBLIC_SEED_CSV

        rc = import_main([str(PUBLIC_SEED_CSV), "--dry-run"])
        assert rc == 0
        assert "未写库" in capsys.readouterr().out
