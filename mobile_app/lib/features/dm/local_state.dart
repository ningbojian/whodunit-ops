/// DM 端离线时的乐观状态转移。
///
/// 只复制服务端状态机中“用户可见”的部分，用于断网时继续显示与排队；
/// 恢复网络后以 `/api/dm/run/sync` 的返回为准。这里没有也不会产生
/// “到点自动 next”的路径：所有转移都必须由 DM 的显式动作触发。
class LocalTransition {
  const LocalTransition({
    required this.run,
    required this.stages,
    this.code,
    this.error,
  });

  final Map<String, dynamic> run;
  final List<Map<String, dynamic>> stages;
  final String? code;
  final String? error;

  bool get ok => error == null;
}

int? _intOf(Object? value) {
  if (value is num) {
    return value.toInt();
  }
  return int.tryParse('${value ?? ''}');
}

String _textOf(Object? value) => '${value ?? ''}'.trim();

DateTime? _parse(Object? value) {
  final text = _textOf(value);
  if (text.isEmpty) {
    return null;
  }
  final parsed = DateTime.tryParse(text);
  if (parsed == null) {
    return null;
  }
  return parsed.isUtc ? parsed : parsed.toUtc();
}

String _iso(DateTime value) => value.toUtc().toIso8601String();

Map<String, dynamic>? _stageAt(List<Map<String, dynamic>> stages, int? seq) {
  if (seq == null) {
    return null;
  }
  for (final stage in stages) {
    if (_intOf(stage['seq']) == seq) {
      return stage;
    }
  }
  return null;
}

void _markStages(List<Map<String, dynamic>> stages, int? seq) {
  for (final stage in stages) {
    final current = _intOf(stage['seq']);
    stage['is_current'] = current == seq;
    if (seq == null) {
      stage['state'] = 'upcoming';
    } else if (current == null) {
      stage['state'] = 'upcoming';
    } else if (current < seq) {
      stage['state'] = 'completed';
    } else if (current == seq) {
      stage['state'] = 'current';
    } else {
      stage['state'] = 'upcoming';
    }
  }
}

int _durationSeconds(Map<String, dynamic>? stage) {
  final minutes = _intOf(stage?['duration_minutes']) ?? 240;
  return (minutes <= 0 ? 240 : minutes) * 60;
}

LocalTransition _fail(String code, String message, Map<String, dynamic> run, List<Map<String, dynamic>> stages) {
  return LocalTransition(
    run: Map<String, dynamic>.from(run),
    stages: stages.map((item) => Map<String, dynamic>.from(item)).toList(),
    code: code,
    error: message,
  );
}

LocalTransition applyLocalAction({
  required Map<String, dynamic> run,
  required List<Map<String, dynamic>> stages,
  required String action,
  required DateTime atUtc,
  int? minutes,
  String? mode,
}) {
  final next = Map<String, dynamic>.from(run);
  final nextStages = stages.map((item) => Map<String, dynamic>.from(item)).toList();
  final status = _textOf(next['status']);
  final version = _intOf(next['state_version']) ?? 0;
  final currentSeq = _intOf(next['current_seq']);

  void commit() {
    next['state_version'] = version + 1;
    next['updated_at'] = _iso(atUtc);
    next['last_activity_at'] = _iso(atUtc);
  }

  switch (action) {
    case 'start':
      if (status != 'pending') {
        return _fail('invalid_action', '场控已经开始或结束。', next, nextStages);
      }
      if (nextStages.isEmpty) {
        return _fail('template_missing', '该剧本没有启用的阶段模板。', next, nextStages);
      }
      final first = _stageAt(nextStages, 1) ?? nextStages.first;
      final duration = _durationSeconds(first);
      next['status'] = 'active';
      next['current_seq'] = 1;
      next['current_stage_id'] = _textOf(first['stage_id']);
      next['stage_started_at'] = _iso(atUtc);
      next['stage_ends_at'] = _iso(atUtc.add(Duration(seconds: duration)));
      next['paused_at'] = null;
      next['paused_total_seconds'] = 0;
      next['remaining_seconds_at_pause'] = null;
      next['remaining_seconds'] = duration;
      next['overtime_seconds'] = 0;
      next['paused'] = false;
      _markStages(nextStages, 1);
      commit();
      return LocalTransition(run: next, stages: nextStages);

    case 'next':
    case 'previous':
      if (status == 'paused') {
        return _fail('run_paused', '场控已暂停，请先继续。', next, nextStages);
      }
      if (status != 'active') {
        return _fail('invalid_action', '只有进行中的场控可以切幕。', next, nextStages);
      }
      if (nextStages.isEmpty || currentSeq == null) {
        return _fail('template_missing', '本场控没有可用的阶段快照。', next, nextStages);
      }
      final targetSeq = currentSeq + (action == 'next' ? 1 : -1);
      if (targetSeq > nextStages.length) {
        return _fail('at_last_stage', '已经是最后一幕，如需结束请点结束场控。', next, nextStages);
      }
      if (targetSeq < 1) {
        return _fail('at_first_stage', '已经是第一幕。', next, nextStages);
      }
      final target = _stageAt(nextStages, targetSeq);
      if (target == null) {
        return _fail('template_missing', '阶段快照不完整。', next, nextStages);
      }
      final duration = _durationSeconds(target);
      next['current_seq'] = targetSeq;
      next['current_stage_id'] = _textOf(target['stage_id']);
      next['stage_started_at'] = _iso(atUtc);
      next['stage_ends_at'] = _iso(atUtc.add(Duration(seconds: duration)));
      next['paused_at'] = null;
      next['remaining_seconds_at_pause'] = null;
      next['remaining_seconds'] = duration;
      next['overtime_seconds'] = 0;
      next['paused'] = false;
      _markStages(nextStages, targetSeq);
      commit();
      return LocalTransition(run: next, stages: nextStages);

    case 'pause':
      if (status != 'active') {
        return _fail('invalid_action', '只有进行中的场控可以暂停。', next, nextStages);
      }
      final ends = _parse(next['stage_ends_at']);
      final remaining = ends == null
          ? (_intOf(next['remaining_seconds']) ?? 0)
          : ends.difference(atUtc).inSeconds;
      final frozen = remaining < 0 ? 0 : remaining;
      next['status'] = 'paused';
      next['paused'] = true;
      next['paused_at'] = _iso(atUtc);
      next['remaining_seconds_at_pause'] = frozen;
      next['remaining_seconds'] = frozen;
      next['overtime_seconds'] = remaining < 0 ? -remaining : 0;
      commit();
      return LocalTransition(run: next, stages: nextStages);

    case 'resume':
      if (status != 'paused') {
        return _fail('invalid_action', '只有已暂停的场控可以继续。', next, nextStages);
      }
      final pausedAt = _parse(next['paused_at']);
      final pausedSeconds = pausedAt == null
          ? 0
          : atUtc.difference(pausedAt).inSeconds.clamp(0, 1 << 31);
      final remaining = _intOf(next['remaining_seconds_at_pause']) ??
          _intOf(next['remaining_seconds']) ??
          0;
      next['status'] = 'active';
      next['paused'] = false;
      next['stage_ends_at'] = _iso(atUtc.add(Duration(seconds: remaining)));
      next['paused_at'] = null;
      next['paused_total_seconds'] =
          (_intOf(next['paused_total_seconds']) ?? 0) + pausedSeconds;
      next['remaining_seconds_at_pause'] = null;
      next['remaining_seconds'] = remaining;
      next['overtime_seconds'] = 0;
      commit();
      return LocalTransition(run: next, stages: nextStages);

    case 'extend':
      if (status != 'active' && status != 'paused') {
        return _fail('invalid_action', '只有进行中或暂停的场控可以延长。', next, nextStages);
      }
      final amount = minutes ?? 0;
      if (amount < 1 || amount > 20) {
        return _fail('invalid_action', '延长分钟数必须在 1..20。', next, nextStages);
      }
      if (status == 'active') {
        final ends = _parse(next['stage_ends_at']) ??
            atUtc.add(Duration(seconds: _intOf(next['remaining_seconds']) ?? 0));
        next['stage_ends_at'] = _iso(ends.add(Duration(minutes: amount)));
        final remaining = _intOf(next['remaining_seconds']);
        if (remaining != null) {
          next['remaining_seconds'] = remaining + amount * 60;
        }
      } else {
        next['remaining_seconds_at_pause'] =
            (_intOf(next['remaining_seconds_at_pause']) ?? 0) + amount * 60;
        next['remaining_seconds'] =
            (_intOf(next['remaining_seconds']) ?? 0) + amount * 60;
      }
      commit();
      return LocalTransition(run: next, stages: nextStages);

    case 'finish':
      if (status != 'active' && status != 'paused') {
        return _fail('invalid_action', '只有进行中或暂停的场控可以结束。', next, nextStages);
      }
      next['status'] = 'completed';
      next['paused'] = false;
      next['finished_at'] = _iso(atUtc);
      next['remaining_seconds'] = 0;
      next['overtime_seconds'] = 0;
      commit();
      return LocalTransition(run: next, stages: nextStages);

    case 'mode':
      if (status != 'pending' && status != 'active' && status != 'paused') {
        return _fail('invalid_action', '场控已经结束。', next, nextStages);
      }
      final target = _textOf(mode);
      if (target != 'software' && target != 'hardware') {
        return _fail('invalid_action', '场控模式不合法。', next, nextStages);
      }
      next['mode'] = target;
      if (target == 'software') {
        next['hardware_degraded'] = false;
      }
      commit();
      return LocalTransition(run: next, stages: nextStages);

    case 'hardware_retry':
      if (status != 'active' && status != 'paused') {
        return _fail('invalid_action', '只有进行中或暂停的场控可以重试硬件。', next, nextStages);
      }
      if (_textOf(next['mode']) != 'hardware') {
        return _fail('invalid_action', '当前不是硬件模式。', next, nextStages);
      }
      commit();
      return LocalTransition(run: next, stages: nextStages, code: 'offline_no_hardware');

    default:
      return _fail('invalid_action', '未知的场控动作。', next, nextStages);
  }
}

/// 当前剩余秒数；`null` 表示尚未开始。暂停时使用冻结值。
int? remainingSeconds(Map<String, dynamic> run, DateTime nowUtc) {
  final status = _textOf(run['status']);
  if (status == 'pending') {
    return null;
  }
  if (status == 'completed' || status == 'abandoned') {
    return 0;
  }
  if (status == 'paused') {
    return _intOf(run['remaining_seconds_at_pause']) ??
        _intOf(run['remaining_seconds']) ??
        0;
  }
  final ends = _parse(run['stage_ends_at']);
  if (ends == null) {
    return _intOf(run['remaining_seconds']);
  }
  final seconds = ends.difference(nowUtc).inSeconds;
  return seconds < 0 ? 0 : seconds;
}

int overtimeSeconds(Map<String, dynamic> run, DateTime nowUtc) {
  final status = _textOf(run['status']);
  if (status == 'paused') {
    final pausedAt = _parse(run['paused_at']);
    final ends = _parse(run['stage_ends_at']);
    if (pausedAt != null && ends != null) {
      final diff = pausedAt.difference(ends).inSeconds;
      return diff > 0 ? diff : 0;
    }
    return 0;
  }
  if (status != 'active') {
    return 0;
  }
  final ends = _parse(run['stage_ends_at']);
  if (ends == null) {
    return 0;
  }
  final diff = nowUtc.difference(ends).inSeconds;
  return diff > 0 ? diff : 0;
}

Map<String, dynamic>? currentStageOf(List<Map<String, dynamic>> stages) {
  for (final stage in stages) {
    if (stage['is_current'] == true) {
      return stage;
    }
  }
  return null;
}
