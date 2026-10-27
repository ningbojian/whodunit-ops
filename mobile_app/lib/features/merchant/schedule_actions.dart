import '../../core/api_client.dart';

/// POST /api/app/schedule/resolve——冲突按钮、场次详情页共用的换房/换 DM 调用。
///
/// choice 白名单由服务端裁定（switch_room / switch_dm / keep_unassigned）；
/// targetId 为空时不携带 target_id 字段（keep_unassigned 等无需目标的选项）。
Future<Map<String, dynamic>> resolveSession(
  ApiClient api, {
  required String sessionId,
  required String choice,
  String? targetId,
}) {
  return api.post(
    '/api/app/schedule/resolve',
    body: {
      'session_id': sessionId,
      'choice': choice,
      if (targetId != null && targetId.isNotEmpty) 'target_id': targetId,
    },
  );
}
