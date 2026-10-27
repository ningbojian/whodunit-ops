/// 服务端时间 + 本地单调时钟。
///
/// 场控剩余时间必须用服务端时间计算，不能信任设备时钟：
/// 用户改系统时间不应让阶段提前超时或续命。
class ServerClock {
  Duration _offset = Duration.zero;
  DateTime? _lastSync;

  /// [serverNow] 来自响应里的 `server_now`（ISO 8601）。
  void syncFromIso(String? serverNow) {
    if (serverNow == null || serverNow.isEmpty) {
      return;
    }
    final parsed = DateTime.tryParse(serverNow);
    if (parsed == null) {
      return;
    }
    final utc = parsed.isUtc ? parsed : parsed.toUtc();
    _offset = utc.difference(DateTime.now().toUtc());
    _lastSync = DateTime.now();
  }

  DateTime nowUtc() => DateTime.now().toUtc().add(_offset);

  /// 服务端返回的时间戳可能是带时区 ISO，也可能是无时区的 UTC。
  DateTime parseUtc(String? value) {
    if (value == null || value.isEmpty) {
      return nowUtc();
    }
    final parsed = DateTime.tryParse(value);
    if (parsed == null) {
      return nowUtc();
    }
    return parsed.isUtc ? parsed : DateTime.utc(
      parsed.year,
      parsed.month,
      parsed.day,
      parsed.hour,
      parsed.minute,
      parsed.second,
    );
  }

  DateTime? get lastSync => _lastSync;
}
