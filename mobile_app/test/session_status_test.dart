import 'package:flutter_test/flutter_test.dart';
import 'package:jbs_operator/ui/widgets.dart';

void main() {
  group('sessionStatusKey', () {
    test('中文三态映射为稳定 key', () {
      expect(sessionStatusKey('待开场'), 'pending');
      expect(sessionStatusKey('已开场'), 'started');
      expect(sessionStatusKey('已取消'), 'cancelled');
    });

    test('未知值原样透传', () {
      expect(sessionStatusKey('改期中'), '改期中');
      expect(sessionStatusKey('assigned'), 'assigned');
    });

    test('空值走 fallback（缺状态按待开场处理）', () {
      expect(sessionStatusKey(null, fallback: '待开场'), 'pending');
      expect(sessionStatusKey('', fallback: '待开场'), 'pending');
      expect(sessionStatusKey('  ', fallback: '待开场'), 'pending');
      expect(sessionStatusKey(null), '');
    });

    test('前后空白不影响映射', () {
      expect(sessionStatusKey(' 已取消 '), 'cancelled');
    });
  });
}
