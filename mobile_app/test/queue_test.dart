import 'package:flutter_test/flutter_test.dart';
import 'package:jbs_operator/core/queue.dart';
import 'package:path/path.dart' as p;
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

PendingAction action(
  String id, {
  String type = 'next',
  int version = 1,
  int? minutes,
  String? mode,
}) {
  return PendingAction(
    id: id,
    action: type,
    at: '2026-10-21T12:00:00.000Z',
    expectedVersion: version,
    minutes: minutes,
    mode: mode,
  );
}

void main() {
  sqfliteFfiInit();
  databaseFactory = databaseFactoryFfi;

  late String databaseName;
  late ActionQueue queue;

  setUp(() {
    databaseName =
        'dm_queue_test_${DateTime.now().microsecondsSinceEpoch}.db';
    queue = ActionQueue(databaseName: databaseName, maxItems: 3);
  });

  tearDown(() async {
    await queue.close();
    final root = await databaseFactory.getDatabasesPath();
    await databaseFactory.deleteDatabase(p.join(root, databaseName));
  });

  test('按入队顺序取出，并保留版本与参数', () async {
    expect(await queue.enqueue(action('a', version: 1)), isTrue);
    await Future<void>.delayed(const Duration(milliseconds: 2));
    expect(
      await queue.enqueue(action('b', type: 'extend', version: 2, minutes: 5)),
      isTrue,
    );
    await Future<void>.delayed(const Duration(milliseconds: 2));
    expect(
      await queue.enqueue(action('c', type: 'mode', version: 3, mode: 'hardware')),
      isTrue,
    );

    final pending = await queue.pending();
    expect(pending.map((item) => item.id), ['a', 'b', 'c']);
    expect(pending[1].minutes, 5);
    expect(pending[2].mode, 'hardware');
    expect(pending[2].expectedVersion, 3);
  });

  test('达到上限后拒绝继续离线推进', () async {
    for (final id in ['a', 'b', 'c']) {
      expect(await queue.enqueue(action(id)), isTrue);
    }
    expect(await queue.enqueue(action('d')), isFalse);
    expect((await queue.pending()).length, 3);
  });

  test('同一 client_event_id 重复入队不会产生两条', () async {
    expect(await queue.enqueue(action('same', version: 1)), isTrue);
    expect(
      await queue.enqueue(action('same', type: 'pause', version: 2)),
      isTrue,
    );
    final pending = await queue.pending();
    expect(pending.length, 1);
    expect(pending.single.action, 'pause');
    expect(pending.single.expectedVersion, 2);
  });

  test('removeAll 只删除指定动作，clear 清空', () async {
    for (final id in ['a', 'b', 'c']) {
      await queue.enqueue(action(id));
    }
    await queue.removeAll(['a', 'c']);
    expect((await queue.pending()).map((item) => item.id), ['b']);

    await queue.clear();
    expect(await queue.pending(), isEmpty);
  });

  test('请求体只带非空的可选参数', () {
    final base = action('a').toRequest();
    expect(base['client_event_id'], 'a');
    expect(base.containsKey('minutes'), isFalse);
    expect(base.containsKey('mode'), isFalse);

    final extend = action('b', type: 'extend', minutes: 3).toRequest();
    expect(extend['minutes'], 3);
    expect(extend.containsKey('mode'), isFalse);

    final mode = action('c', type: 'mode', mode: 'software').toRequest();
    expect(mode['mode'], 'software');
    expect(mode.containsKey('minutes'), isFalse);
  });
}
