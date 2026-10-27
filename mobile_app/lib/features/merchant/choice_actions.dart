import '../../core/api_client.dart';
import '../../ui/widgets.dart' show textOf;

/// 聊天按钮 → 业务 API 的映射（纯函数，按钮直连 API 不经模型）。
///
/// 两类动作：
///  - resolve：排期冲突的可执行选项（换房 / 换 DM / 先不排）；
///  - quick_reply：纯文本追问，重发消息，没有 API 路径。
///
/// 服务端白名单在 chat_choices.build_choices 与 /api/app/schedule/resolve；
/// 这里只做请求形状组装，不重复校验。

/// 该动作对应的 API 路径；quick_reply 返回 null（走重发文本）。
String? apiPathFor(String action) {
  switch (action) {
    case 'resolve':
      return '/api/app/schedule/resolve';
    case 'quick_reply':
      return null;
    default:
      return null;
  }
}

bool isKnownAction(String action) => apiPathFor(action) != null || action == 'quick_reply';

/// 组装请求体；字段缺失时返回 null（调用方按无效按钮处理）。
Map<String, dynamic>? bodyFor(Map<String, dynamic> button) {
  final action = textOf(button['action']);
  final raw = button['payload'];
  final payload = raw is Map ? Map<String, dynamic>.from(raw) : const <String, dynamic>{};
  switch (action) {
    case 'resolve':
      final sessionId = textOf(payload['session_id']);
      final choice = textOf(payload['choice']);
      if (sessionId.isEmpty || choice.isEmpty) {
        return null;
      }
      final targetId = textOf(payload['target_id']);
      return {
        'session_id': sessionId,
        'choice': choice,
        if (targetId.isNotEmpty) 'target_id': targetId,
      };
    default:
      return null;
  }
}

/// quick_reply 的重发文本；其他动作返回 null。
String? quickReplyText(Map<String, dynamic> button) {
  if (textOf(button['action']) != 'quick_reply') {
    return null;
  }
  final raw = button['payload'];
  final payload = raw is Map ? raw : const {};
  final text = textOf(payload['text']);
  return text.isEmpty ? null : text;
}

/// 执行一次按钮动作：quick_reply 由调用方重发文本，其余直连业务 API。
/// 返回服务端 JSON 信封（含 ok/status 等），失败抛 [ApiException]。
Future<Map<String, dynamic>> runChoiceAction(
  ApiClient api,
  Map<String, dynamic> button,
) async {
  final action = textOf(button['action']);
  final path = apiPathFor(action);
  final body = bodyFor(button);
  if (path == null || body == null) {
    throw ApiException('bad_action', '按钮动作无效，请刷新后重试。');
  }
  return api.post(path, body: body);
}

/// 从 choice group 的一条 button 提取可展示的 API 回执摘要；
/// 不伪造成功文案，只转述服务端字段。
String summarizeChoiceResult(Map<String, dynamic> result) {
  final status = textOf(result['status']);
  final message = textOf(result['message']);
  if (message.isNotEmpty) {
    return message;
  }
  if (status.isNotEmpty) {
    return '服务端返回：$status';
  }
  if (result['ok'] == true) {
    return '已提交。';
  }
  return '服务端未返回可读结果。';
}
