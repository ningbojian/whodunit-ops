import 'package:flutter_test/flutter_test.dart';
import 'package:jbs_operator/features/merchant/choice_actions.dart';

void main() {
  group('apiPathFor', () {
    test('resolve 指向排期解决端点', () {
      expect(apiPathFor('resolve'), '/api/app/schedule/resolve');
    });

    test('quick_reply 无 API 路径（走重发文本）', () {
      expect(apiPathFor('quick_reply'), isNull);
    });

    test('未知动作返回 null', () {
      expect(apiPathFor('unknown_action'), isNull);
      expect(apiPathFor(''), isNull);
    });
  });

  group('bodyFor', () {
    test('resolve 带 session_id、choice，有 target_id 时附带', () {
      final body = bodyFor({
        'action': 'resolve',
        'payload': {
          'session_id': 's1',
          'choice': 'switch_room',
          'target_id': 'room-2',
        },
      });
      expect(body, {
        'session_id': 's1',
        'choice': 'switch_room',
        'target_id': 'room-2',
      });
    });

    test('resolve 无 target_id 时不带该键（keep_unassigned 等）', () {
      final body = bodyFor({
        'action': 'resolve',
        'payload': {'session_id': 's1', 'choice': 'keep_unassigned'},
      });
      expect(body, {'session_id': 's1', 'choice': 'keep_unassigned'});
      expect(body!.containsKey('target_id'), isFalse);
    });

    test('字段缺失返回 null（不发半截请求）', () {
      expect(
        bodyFor({
          'action': 'resolve',
          'payload': {'session_id': 's1'},
        }),
        isNull,
      );
    });

    test('quick_reply 与未知动作返回 null', () {
      expect(
        bodyFor({
          'action': 'quick_reply',
          'payload': {'text': '你好'},
        }),
        isNull,
      );
      expect(bodyFor({'action': 'nope'}), isNull);
    });
  });

  group('quickReplyText', () {
    test('quick_reply 返回文本', () {
      expect(
        quickReplyText({
          'action': 'quick_reply',
          'payload': {'text': '是《年轮》吗？'},
        }),
        '是《年轮》吗？',
      );
    });

    test('其他动作返回 null', () {
      expect(
        quickReplyText({
          'action': 'resolve',
          'payload': {'text': '不应被取'},
        }),
        isNull,
      );
    });
  });

  group('summarizeChoiceResult', () {
    test('优先转述服务端 message', () {
      expect(summarizeChoiceResult({'ok': true, 'message': '已换到 2 号房'}),
          '已换到 2 号房');
    });

    test('无 message 时展示 status', () {
      expect(summarizeChoiceResult({'ok': true, 'status': 'promoted'}),
          '服务端返回：promoted');
    });

    test('都不缺省时兜底', () {
      expect(summarizeChoiceResult({'ok': true}), '已提交。');
    });
  });
}
