import 'package:flutter_test/flutter_test.dart';
import 'package:jbs_operator/features/merchant/schedule_assign_sheet.dart';

void main() {
  group('buildApplyPayload', () {
    test('勾选的 proposals 映射为 apply 请求体，带 expect 键与 confirm', () {
      final payload = buildApplyPayload([
        {
          'session_id': 's1',
          'before': {'room_id': '', 'dm_id': ''},
          'after': {'room_id': 'r1', 'dm_id': 'd1'},
        },
      ]);
      expect(payload['confirm'], true);
      expect(payload['assignments'], [
        {
          'session_id': 's1',
          'room_id': 'r1',
          'dm_id': 'd1',
          'expect_room_id': '',
          'expect_dm_id': '',
        },
      ]);
    });

    test('before 有值时 expect 带原 id（预览过期保护）', () {
      final payload = buildApplyPayload([
        {
          'session_id': 's2',
          'before': {
            'room_id': 'r9',
            'dm_id': 'd9',
          },
          'after': {
            'room_id': 'r2',
            'dm_id': 'd2',
          },
        },
      ]);
      final assignments = payload['assignments'] as List;
      expect(assignments.single, {
        'session_id': 's2',
        'room_id': 'r2',
        'dm_id': 'd2',
        'expect_room_id': 'r9',
        'expect_dm_id': 'd9',
      });
    });

    test('多条按原顺序提交', () {
      final payload = buildApplyPayload([
        {
          'session_id': 'a',
          'after': {'room_id': 'r1'},
        },
        {
          'session_id': 'b',
          'after': {'dm_id': 'd2'},
        },
      ]);
      final assignments = (payload['assignments'] as List).cast<Map>();
      expect(assignments.map((item) => item['session_id']), ['a', 'b']);
      expect(assignments.first, {
        'session_id': 'a',
        'room_id': 'r1',
        'dm_id': '',
        'expect_room_id': '',
        'expect_dm_id': '',
      });
      expect(assignments.last['dm_id'], 'd2');
      expect(assignments.last['room_id'], '');
    });

    test('before/after 缺失时按空串处理，不发 null', () {
      final payload = buildApplyPayload([
        {'session_id': 's3'},
      ]);
      final assignments = (payload['assignments'] as List).cast<Map>();
      expect(assignments.single.containsKey('room_id'), isTrue);
      expect(assignments.single['room_id'], '');
      expect(assignments.single['dm_id'], '');
      expect(assignments.single['expect_room_id'], '');
      expect(assignments.single['expect_dm_id'], '');
    });

    test('空选择也返回合法形状（confirm + 空 assignments）', () {
      final payload = buildApplyPayload([]);
      expect(payload['confirm'], true);
      expect(payload['assignments'], isEmpty);
    });
  });

  group('assignmentChangeText', () {
    test('完全未分配的 before 显示「未分配」', () {
      expect(
        assignmentChangeText({
          'before': {'room_id': '', 'dm_id': ''},
          'after': {
            'room_name': 'A 房',
            'dm_name': '小北',
          },
        }),
        '房间：未分配 → A 房 / DM：未分配 → 小北',
      );
    });

    test('有原分配时显示原名 → 新名', () {
      expect(
        assignmentChangeText({
          'before': {
            'room_name': 'B 房',
            'dm_name': '阿绿',
          },
          'after': {
            'room_name': 'C 房',
            'dm_name': '小北',
          },
        }),
        '房间：B 房 → C 房 / DM：阿绿 → 小北',
      );
    });

    test('名称缺失回退到 id', () {
      expect(
        assignmentChangeText({
          'before': {'room_id': 'r1', 'dm_id': 'd1'},
          'after': {'room_id': 'r2', 'dm_id': 'd2'},
        }),
        '房间：r1 → r2 / DM：d1 → d2',
      );
    });
  });
}
