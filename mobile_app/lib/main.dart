import 'package:flutter/material.dart';

import 'app.dart';
import 'core/api_client.dart';
import 'core/auth_store.dart';
import 'core/clock.dart';
import 'core/notifications.dart';
import 'core/queue.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  const baseUrl = String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'http://127.0.0.1:8000',
  );
  final notifications = NotificationService();
  await notifications.init();
  runApp(
    JbsApp(
      api: ApiClient(baseUrl: baseUrl),
      auth: AuthStore(),
      queue: ActionQueue(),
      clock: ServerClock(),
      notifications: notifications,
    ),
  );
}
