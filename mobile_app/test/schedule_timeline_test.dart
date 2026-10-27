import 'package:flutter_test/flutter_test.dart';
import 'package:jbs_operator/features/merchant/schedule_timeline.dart';

void main() {
  group('timelineOffsetMinutes', () {
    test('10:00 为视窗原点 0', () {
      expect(timelineOffsetMinutes(DateTime(2026, 10, 22, 10, 0)), 0);
    });

    test('19:00 → 540', () {
      expect(timelineOffsetMinutes(DateTime(2026, 10, 22, 19, 0)), 540);
    });

    test('次日凌晨 01:00 → 900（+14h 跨午夜段）', () {
      expect(timelineOffsetMinutes(DateTime(2026, 10, 23, 1, 0)), 900);
    });

    test('00:30 → 870', () {
      expect(timelineOffsetMinutes(DateTime(2026, 10, 23, 0, 30)), 870);
    });

    test('23:59 → 839', () {
      expect(timelineOffsetMinutes(DateTime(2026, 10, 22, 23, 59)), 839);
    });

    test('04:00 恰在视窗末界（=1080 → 不可见）', () {
      expect(timelineOffsetMinutes(DateTime(2026, 10, 23, 4, 0)), 1080);
      expect(timelineVisible(1080), isFalse);
    });

    test('09:59 为负（视窗外）', () {
      expect(timelineOffsetMinutes(DateTime(2026, 10, 22, 9, 59)), -1);
      expect(timelineVisible(-1), isFalse);
    });

    test('05:00 为负（视窗外）', () {
      expect(timelineOffsetMinutes(DateTime(2026, 10, 22, 5, 0)), -300);
    });

    test('timelineVisible：0 与 540 在视窗内', () {
      expect(timelineVisible(0), isTrue);
      expect(timelineVisible(540), isTrue);
      expect(timelineVisible(timelineSpanMinutes - 1), isTrue);
    });
  });

  group('blockTop', () {
    test('正常偏移按 pxPerMinute 换算', () {
      expect(blockTop(540), 540 * 1.4);
      expect(blockTop(0), 0.0);
    });

    test('负偏移 clamp 到视窗顶', () {
      expect(blockTop(-300), 0.0);
    });

    test('超出上限 clamp 到视窗底', () {
      expect(blockTop(2000), timelineSpanMinutes * 1.4);
    });

    test('自定义 pxPerMinute', () {
      expect(blockTop(60, pxPerMinute: 2.0), 120.0);
    });
  });

  group('clampDurationMinutes', () {
    test('null → 45', () {
      expect(clampDurationMinutes(null), 45);
    });

    test('<= 0 → 45', () {
      expect(clampDurationMinutes(0), 45);
      expect(clampDurationMinutes(-10), 45);
    });

    test('正常值透传', () {
      expect(clampDurationMinutes(300), 300);
      expect(clampDurationMinutes(45), 45);
    });

    test('下限 15、上限 720', () {
      expect(clampDurationMinutes(5), 15);
      expect(clampDurationMinutes(5000), 720);
    });
  });

  group('timelineDateKey', () {
    test('凌晨 01:00 归入前一天（营业日分组）', () {
      expect(timelineDateKey('2026-10-24 01:00'), '2026-10-23');
    });

    test('00:30 归入前一天', () {
      expect(timelineDateKey('2026-10-24 00:30'), '2026-10-23');
    });

    test('04:59 仍归前一天，05:00 起回自然日', () {
      expect(timelineDateKey('2026-10-24 04:59'), '2026-10-23');
      expect(timelineDateKey('2026-10-24 05:00'), '2026-10-24');
    });

    test('白天与深夜场次保持自然日', () {
      expect(timelineDateKey('2026-10-22 10:00'), '2026-10-22');
      expect(timelineDateKey('2026-10-22 23:30'), '2026-10-22');
    });

    test('跨月：11-01 01:00 → 10-31', () {
      expect(timelineDateKey('2026-11-01 01:00'), '2026-10-31');
    });

    test('无时刻部分的日期串保持不变', () {
      expect(timelineDateKey('2026-10-22'), '2026-10-22');
    });

    test('解析失败回退原串', () {
      expect(timelineDateKey(''), '');
      expect(timelineDateKey('待定'), '待定');
    });
  });

  group('timelineHourMarks', () {
    test('覆盖 10..23 与次日 0..4，共 19 项', () {
      expect(timelineHourMarks.length, 19);
      expect(timelineHourMarks.first, 10);
      expect(timelineHourMarks.last, 4);
      expect(timelineHourMarks.sublist(14), [0, 1, 2, 3, 4]);
    });

    test('偏移严格递增，4 恰在视窗末界', () {
      final offsets = [
        for (final hour in timelineHourMarks)
          timelineOffsetMinutes(DateTime(2026, 1, 1, hour)),
      ];
      for (var i = 1; i < offsets.length; i++) {
        expect(offsets[i], greaterThan(offsets[i - 1]));
      }
      expect(offsets.last, timelineSpanMinutes);
    });
  });

  group('timelineBlockTop', () {
    test('视窗内块不位移（相邻深夜场次不虚构重叠）', () {
      // 03:45 → offset 1065：自然 top≈1491，不得被压到 1449。
      expect(
        timelineBlockTop(1065, blockHeight: 63),
        moreOrLessEquals(1065 * 1.4),
      );
    });

    test('04:00 之后贴底保持可见', () {
      expect(timelineBlockTop(1080, blockHeight: 63), moreOrLessEquals(1512 - 63));
      expect(timelineBlockTop(1110, blockHeight: 63), moreOrLessEquals(1512 - 63));
    });

    test('块高超过视窗时贴顶为 0', () {
      expect(timelineBlockTop(1110, blockHeight: 2000), 0.0);
    });
  });
}
