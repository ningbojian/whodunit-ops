import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:flutter_timezone/flutter_timezone.dart';
import 'package:timezone/data/latest.dart' as tzdata;
import 'package:timezone/timezone.dart' as tz;

/// 阶段提醒。只负责本地通知，不改变场控状态。
///
/// 到点通知只是提醒 DM：绝不会触发“下一幕”。暂停时取消当前阶段通知。
class NotificationService {
  NotificationService({FlutterLocalNotificationsPlugin? plugin})
      : _plugin = plugin ?? FlutterLocalNotificationsPlugin();

  final FlutterLocalNotificationsPlugin _plugin;
  bool _ready = false;

  static const _channelId = 'dm_stage_reminders';
  static const _details = NotificationDetails(
    android: AndroidNotificationDetails(
      _channelId,
      '场控阶段提醒',
      channelDescription: '阶段提前提醒与到点提醒；不会自动推进剧情。',
      importance: Importance.high,
      priority: Priority.high,
      playSound: true,
      enableVibration: true,
    ),
    iOS: DarwinNotificationDetails(
      presentAlert: true,
      presentBadge: true,
      presentSound: true,
    ),
  );

  static int warnId(int seq) => 4100 + seq;
  static int dueId(int seq) => 4200 + seq;

  Future<void> init() async {
    if (_ready) {
      return;
    }
    tzdata.initializeTimeZones();
    try {
      final name = await FlutterTimezone.getLocalTimezone();
      tz.setLocalLocation(tz.getLocation(name));
    } catch (_) {
      // 时区名不可用时保留默认 UTC；前台倒计时仍按服务端时间计算。
      tz.setLocalLocation(tz.getLocation('UTC'));
    }
    const settings = InitializationSettings(
      android: AndroidInitializationSettings('@mipmap/ic_launcher'),
      iOS: DarwinInitializationSettings(
        requestAlertPermission: true,
        requestBadgePermission: false,
        requestSoundPermission: true,
      ),
    );
    try {
      await _plugin.initialize(settings);
      final android = _plugin.resolvePlatformSpecificImplementation<
          AndroidFlutterLocalNotificationsPlugin>();
      await android?.requestNotificationsPermission();
      _ready = true;
    } catch (_) {
      _ready = false;
    }
  }

  /// 为当前阶段安排“提前提醒 + 到点提醒”。重复调用会先取消同阶段旧通知。
  Future<void> scheduleStage({
    required int seq,
    required DateTime endsAtUtc,
    required int warnBeforeSeconds,
    String? prompt,
  }) async {
    if (!_ready) {
      return;
    }
    await cancelStage(seq);
    final now = DateTime.now().toUtc();
    final body = (prompt == null || prompt.isEmpty) ? '阶段时间到，请按节奏推进。' : prompt;
    final warnAt = endsAtUtc.subtract(Duration(seconds: warnBeforeSeconds));
    if (warnBeforeSeconds > 0 && warnAt.isAfter(now)) {
      await _zoned(
        warnId(seq),
        '阶段即将到点',
        '还有 ${_minutes(warnBeforeSeconds)}，请留意节奏。',
        warnAt,
      );
    }
    if (endsAtUtc.isAfter(now)) {
      await _zoned(dueId(seq), '阶段时间到', body, endsAtUtc);
    }
  }

  Future<void> cancelStage(int seq) async {
    if (!_ready) {
      return;
    }
    try {
      await _plugin.cancel(warnId(seq));
      await _plugin.cancel(dueId(seq));
    } catch (_) {
      // 通知失败不影响场控。
    }
  }

  Future<void> cancelAll(Iterable<int> seqs) async {
    for (final seq in seqs) {
      await cancelStage(seq);
    }
  }

  Future<void> _zoned(
    int id,
    String title,
    String body,
    DateTime whenUtc,
  ) async {
    try {
      await _plugin.zonedSchedule(
        id,
        title,
        body,
        tz.TZDateTime.from(whenUtc, tz.local),
        _details,
        androidScheduleMode: AndroidScheduleMode.inexactAllowWhileIdle,
        uiLocalNotificationDateInterpretation:
            UILocalNotificationDateInterpretation.absoluteTime,
      );
    } catch (_) {
      // 权限被拒或厂商限制时静默降级；前台仍有倒计时和横幅。
    }
  }

  static String _minutes(int seconds) {
    if (seconds < 60) {
      return '$seconds 秒';
    }
    return '${(seconds / 60).ceil()} 分钟';
  }
}
