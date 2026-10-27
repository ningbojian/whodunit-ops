import 'package:flutter_test/flutter_test.dart';
import 'package:jbs_operator/features/dm/local_state.dart';

Map<String, dynamic> baseRun({
  String status = 'pending',
  String mode = 'software',
  bool hardwareDegraded = false,
}) {
  return <String, dynamic>{
    'run_id': 'run-test',
    'status': status,
    'mode': mode,
    'current_seq': status == 'pending' ? null : 1,
    'paused_total_seconds': 0,
    'paused': status == 'paused',
    'hardware_degraded': hardwareDegraded,
    'state_version': 0,
  };
}

List<Map<String, dynamic>> baseStages() => <Map<String, dynamic>>[
      <String, dynamic>{
        'stage_id': 'stage-1',
        'seq': 1,
        'name': '第一幕',
        'duration_minutes': 10,
        'is_current': false,
        'state': 'upcoming',
      },
      <String, dynamic>{
        'stage_id': 'stage-2',
        'seq': 2,
        'name': '第二幕',
        'duration_minutes': 20,
        'is_current': false,
        'state': 'upcoming',
      },
    ];

void main() {
  final startAt = DateTime.utc(2026, 10, 21, 12);

  group('start', () {
    test('进入第一幕并给出结束时间，原对象不被修改', () {
      final run = baseRun();
      final stages = baseStages();
      final result = applyLocalAction(
        run: run,
        stages: stages,
        action: 'start',
        atUtc: startAt,
      );

      expect(result.ok, isTrue);
      expect(result.run['status'], 'active');
      expect(result.run['current_seq'], 1);
      expect(result.run['current_stage_id'], 'stage-1');
      expect(result.run['stage_started_at'], startAt.toIso8601String());
      expect(
        result.run['stage_ends_at'],
        startAt.add(const Duration(minutes: 10)).toIso8601String(),
      );
      expect(result.run['remaining_seconds'], 600);
      expect(result.run['state_version'], 1);
      expect(result.stages[0]['state'], 'current');
      expect(result.stages[1]['state'], 'upcoming');

      expect(run['status'], 'pending');
      expect(stages[0]['state'], 'upcoming');
      expect(stages[0]['is_current'], isFalse);
    });

    test('没有阶段模板时返回 template_missing', () {
      final result = applyLocalAction(
        run: baseRun(),
        stages: const [],
        action: 'start',
        atUtc: startAt,
      );
      expect(result.ok, isFalse);
      expect(result.code, 'template_missing');
    });
  });

  group('切幕', () {
    test('next 进入第二幕，previous 回到第一幕', () {
      final started = applyLocalAction(
        run: baseRun(),
        stages: baseStages(),
        action: 'start',
        atUtc: startAt,
      );
      final next = applyLocalAction(
        run: started.run,
        stages: started.stages,
        action: 'next',
        atUtc: startAt.add(const Duration(minutes: 3)),
      );

      expect(next.ok, isTrue);
      expect(next.run['current_seq'], 2);
      expect(next.run['state_version'], 2);
      expect(next.stages[0]['state'], 'completed');
      expect(next.stages[1]['state'], 'current');

      final previous = applyLocalAction(
        run: next.run,
        stages: next.stages,
        action: 'previous',
        atUtc: startAt.add(const Duration(minutes: 5)),
      );
      expect(previous.ok, isTrue);
      expect(previous.run['current_seq'], 1);
      expect(previous.stages[0]['state'], 'current');
      expect(previous.stages[1]['state'], 'upcoming');
    });

    test('第一幕之前与最后一幕之后都被拒绝', () {
      final started = applyLocalAction(
        run: baseRun(),
        stages: baseStages(),
        action: 'start',
        atUtc: startAt,
      );
      final atFirst = applyLocalAction(
        run: started.run,
        stages: started.stages,
        action: 'previous',
        atUtc: startAt,
      );
      expect(atFirst.ok, isFalse);
      expect(atFirst.code, 'at_first_stage');

      final second = applyLocalAction(
        run: started.run,
        stages: started.stages,
        action: 'next',
        atUtc: startAt,
      );
      final atLast = applyLocalAction(
        run: second.run,
        stages: second.stages,
        action: 'next',
        atUtc: startAt,
      );
      expect(atLast.ok, isFalse);
      expect(atLast.code, 'at_last_stage');
      expect(atLast.run['status'], 'active');
    });
  });

  group('暂停与继续', () {
    test('暂停冻结剩余时间，暂停中剩余时间不再减少', () {
      final started = applyLocalAction(
        run: baseRun(),
        stages: baseStages(),
        action: 'start',
        atUtc: startAt,
      );
      final paused = applyLocalAction(
        run: started.run,
        stages: started.stages,
        action: 'pause',
        atUtc: startAt.add(const Duration(minutes: 4)),
      );

      expect(paused.ok, isTrue);
      expect(paused.run['status'], 'paused');
      expect(paused.run['paused'], isTrue);
      expect(paused.run['remaining_seconds_at_pause'], 360);
      expect(paused.run['remaining_seconds'], 360);
      expect(paused.run['overtime_seconds'], 0);
      expect(
        remainingSeconds(paused.run, startAt.add(const Duration(hours: 2))),
        360,
      );
      expect(
        overtimeSeconds(paused.run, startAt.add(const Duration(hours: 2))),
        0,
      );
    });

    test('超时后暂停会冻结超时秒数', () {
      final started = applyLocalAction(
        run: baseRun(),
        stages: baseStages(),
        action: 'start',
        atUtc: startAt,
      );
      final paused = applyLocalAction(
        run: started.run,
        stages: started.stages,
        action: 'pause',
        atUtc: startAt.add(const Duration(minutes: 15)),
      );

      expect(paused.run['remaining_seconds_at_pause'], 0);
      expect(paused.run['overtime_seconds'], 300);
      expect(
        remainingSeconds(paused.run, startAt.add(const Duration(hours: 3))),
        0,
      );
      expect(
        overtimeSeconds(paused.run, startAt.add(const Duration(hours: 3))),
        300,
      );
    });

    test('继续后从冻结点重算结束时间并累计暂停时长', () {
      final started = applyLocalAction(
        run: baseRun(),
        stages: baseStages(),
        action: 'start',
        atUtc: startAt,
      );
      final paused = applyLocalAction(
        run: started.run,
        stages: started.stages,
        action: 'pause',
        atUtc: startAt.add(const Duration(minutes: 4)),
      );
      final resumed = applyLocalAction(
        run: paused.run,
        stages: paused.stages,
        action: 'resume',
        atUtc: startAt.add(const Duration(minutes: 10)),
      );

      expect(resumed.ok, isTrue);
      expect(resumed.run['status'], 'active');
      expect(resumed.run['paused'], isFalse);
      expect(resumed.run['paused_at'], isNull);
      expect(resumed.run['paused_total_seconds'], 360);
      expect(
        resumed.run['stage_ends_at'],
        startAt.add(const Duration(minutes: 16)).toIso8601String(),
      );
      expect(
        remainingSeconds(
          resumed.run,
          startAt.add(const Duration(minutes: 11)),
        ),
        300,
      );
    });

    test('只有已暂停的场控可以继续', () {
      final result = applyLocalAction(
        run: baseRun(status: 'active'),
        stages: baseStages(),
        action: 'resume',
        atUtc: startAt,
      );
      expect(result.ok, isFalse);
      expect(result.code, 'invalid_action');
    });
  });

  group('延长与结束', () {
    test('进行中延长只影响当前阶段结束时间', () {
      final started = applyLocalAction(
        run: baseRun(),
        stages: baseStages(),
        action: 'start',
        atUtc: startAt,
      );
      final extended = applyLocalAction(
        run: started.run,
        stages: started.stages,
        action: 'extend',
        atUtc: startAt.add(const Duration(minutes: 2)),
        minutes: 5,
      );

      expect(extended.ok, isTrue);
      expect(
        extended.run['stage_ends_at'],
        startAt.add(const Duration(minutes: 15)).toIso8601String(),
      );
      expect(extended.run['remaining_seconds'], 900);
      expect(extended.run['state_version'], 2);
    });

    test('暂停中延长只加冻结剩余时间', () {
      final started = applyLocalAction(
        run: baseRun(),
        stages: baseStages(),
        action: 'start',
        atUtc: startAt,
      );
      final paused = applyLocalAction(
        run: started.run,
        stages: started.stages,
        action: 'pause',
        atUtc: startAt.add(const Duration(minutes: 4)),
      );
      final extended = applyLocalAction(
        run: paused.run,
        stages: paused.stages,
        action: 'extend',
        atUtc: startAt.add(const Duration(minutes: 6)),
        minutes: 3,
      );
      expect(extended.run['remaining_seconds_at_pause'], 540);
      expect(extended.run['remaining_seconds'], 540);

      final resumed = applyLocalAction(
        run: extended.run,
        stages: extended.stages,
        action: 'resume',
        atUtc: startAt.add(const Duration(minutes: 10)),
      );
      expect(
        resumed.run['stage_ends_at'],
        startAt.add(const Duration(minutes: 19)).toIso8601String(),
      );
    });

    test('延长分钟数必须在 1..20', () {
      final started = applyLocalAction(
        run: baseRun(),
        stages: baseStages(),
        action: 'start',
        atUtc: startAt,
      );
      for (final minutes in [0, 21, -1]) {
        final result = applyLocalAction(
          run: started.run,
          stages: started.stages,
          action: 'extend',
          atUtc: startAt,
          minutes: minutes,
        );
        expect(result.ok, isFalse);
        expect(result.code, 'invalid_action');
      }
    });

    test('结束只允许进行中或暂停，且不会自动发生', () {
      final pendingFinish = applyLocalAction(
        run: baseRun(),
        stages: baseStages(),
        action: 'finish',
        atUtc: startAt,
      );
      expect(pendingFinish.ok, isFalse);

      final started = applyLocalAction(
        run: baseRun(),
        stages: baseStages(),
        action: 'start',
        atUtc: startAt,
      );
      final finished = applyLocalAction(
        run: started.run,
        stages: started.stages,
        action: 'finish',
        atUtc: startAt.add(const Duration(minutes: 5)),
      );
      expect(finished.run['status'], 'completed');
      expect(finished.run['remaining_seconds'], 0);
      expect(finished.run['finished_at'], isNotNull);

      final afterwards = applyLocalAction(
        run: finished.run,
        stages: finished.stages,
        action: 'next',
        atUtc: startAt.add(const Duration(minutes: 6)),
      );
      expect(afterwards.ok, isFalse);
    });
  });

  group('模式与硬件', () {
    test('切换模式并清除降级标记', () {
      final hardware = applyLocalAction(
        run: baseRun(hardwareDegraded: true),
        stages: baseStages(),
        action: 'mode',
        atUtc: startAt,
        mode: 'hardware',
      );
      expect(hardware.run['mode'], 'hardware');
      expect(hardware.run['hardware_degraded'], isTrue);

      final software = applyLocalAction(
        run: hardware.run,
        stages: hardware.stages,
        action: 'mode',
        atUtc: startAt,
        mode: 'software',
      );
      expect(software.run['mode'], 'software');
      expect(software.run['hardware_degraded'], isFalse);
    });

    test('非法模式被拒绝', () {
      final result = applyLocalAction(
        run: baseRun(),
        stages: baseStages(),
        action: 'mode',
        atUtc: startAt,
        mode: 'magic',
      );
      expect(result.ok, isFalse);
    });

    test('离线重试硬件只返回标记，不冒充已下发', () {
      final started = applyLocalAction(
        run: baseRun(mode: 'hardware', hardwareDegraded: true),
        stages: baseStages(),
        action: 'start',
        atUtc: startAt,
      );
      final retry = applyLocalAction(
        run: started.run,
        stages: started.stages,
        action: 'hardware_retry',
        atUtc: startAt.add(const Duration(minutes: 1)),
      );
      expect(retry.ok, isTrue);
      expect(retry.code, 'offline_no_hardware');
    });
  });

  group('倒计时只读语义', () {
    test('时间越过结束点不会改变阶段、版本或状态', () {
      final started = applyLocalAction(
        run: baseRun(),
        stages: baseStages(),
        action: 'start',
        atUtc: startAt,
      );
      final farFuture = startAt.add(const Duration(hours: 5));

      expect(remainingSeconds(started.run, farFuture), 0);
      expect(overtimeSeconds(started.run, farFuture), 5 * 3600 - 600);
      expect(started.run['status'], 'active');
      expect(started.run['current_seq'], 1);
      expect(started.run['state_version'], 1);
      expect(
        currentStageOf(started.stages)?['stage_id'],
        'stage-1',
      );
    });

    test('服务端暂停载荷没有冻结字段时回退到 remaining_seconds', () {
      final serverPaused = <String, dynamic>{
        'status': 'paused',
        'remaining_seconds': 123,
        'paused_at': startAt.toIso8601String(),
        'stage_ends_at':
            startAt.add(const Duration(minutes: 3)).toIso8601String(),
      };
      expect(
        remainingSeconds(serverPaused, startAt.add(const Duration(hours: 1))),
        123,
      );
      expect(
        overtimeSeconds(serverPaused, startAt.add(const Duration(hours: 1))),
        0,
      );
    });

    test('待开始没有剩余时间', () {
      expect(remainingSeconds(baseRun(), startAt), isNull);
    });
  });
}
