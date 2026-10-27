import 'package:shared_preferences/shared_preferences.dart';
import 'package:uuid/uuid.dart';

/// 本机安装 ID。只用于兑换场控码时做设备维度限流；
/// 服务端再哈希后使用，不保存原始值到任何页面或日志。
Future<String> loadDeviceId() async {
  final prefs = await SharedPreferences.getInstance();
  final existing = prefs.getString('device_id');
  if (existing != null && existing.isNotEmpty) {
    return existing;
  }
  final created = const Uuid().v4();
  await prefs.setString('device_id', created);
  return created;
}
