import 'package:flutter_test/flutter_test.dart';
import 'package:jbs_operator/core/clock.dart';

void main() {
  group('ServerClock.parseUtc', () {
    test('保留带 Z 的 UTC 时间', () {
      final clock = ServerClock();
      final parsed = clock.parseUtc('2026-10-21T12:30:00Z');
      expect(parsed, DateTime.utc(2026, 10, 21, 12, 30));
      expect(parsed.isUtc, isTrue);
    });

    test('把无时区字符串按 UTC 解析，不随设备时区漂移', () {
      final clock = ServerClock();
      final parsed = clock.parseUtc('2026-10-21T12:30:00');
      expect(parsed, DateTime.utc(2026, 10, 21, 12, 30));
    });

    test('空值或非法值回退到当前服务端时间', () {
      final clock = ServerClock();
      final before = DateTime.now().toUtc();
      final fallback = clock.parseUtc(null);
      final after = DateTime.now().toUtc();
      expect(
        fallback.isBefore(before.subtract(const Duration(seconds: 1))),
        isFalse,
      );
      expect(
        fallback.isAfter(after.add(const Duration(seconds: 1))),
        isFalse,
      );

      final invalid = clock.parseUtc('not-a-date');
      expect(
        invalid.isAfter(before.subtract(const Duration(seconds: 1))),
        isTrue,
      );
    });
  });

  group('ServerClock.syncFromIso', () {
    test('同步后按服务端时间偏移，而不是直接信任设备时钟', () {
      final clock = ServerClock();
      final serverNow = DateTime.now().toUtc().add(const Duration(seconds: 30));
      clock.syncFromIso(serverNow.toIso8601String());
      expect(clock.lastSync, isNotNull);
      final drift = clock.nowUtc().difference(serverNow).inSeconds.abs();
      expect(drift, lessThanOrEqualTo(2));
    });

    test('空值不会清除已有偏移', () {
      final clock = ServerClock();
      final serverNow = DateTime.now().toUtc().add(const Duration(seconds: 60));
      clock.syncFromIso(serverNow.toIso8601String());
      clock.syncFromIso(null);
      final drift = clock.nowUtc().difference(serverNow).inSeconds;
      expect(drift.abs(), lessThanOrEqualTo(2));
    });

    test('非法值被忽略', () {
      final clock = ServerClock();
      clock.syncFromIso('garbage');
      expect(clock.lastSync, isNull);
      final drift = clock.nowUtc().difference(DateTime.now().toUtc()).inSeconds;
      expect(drift.abs(), lessThanOrEqualTo(1));
    });
  });
}
