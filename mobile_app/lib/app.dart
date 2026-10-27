import 'package:flutter/material.dart';

import 'core/api_client.dart';
import 'core/auth_store.dart';
import 'core/clock.dart';
import 'core/notifications.dart';
import 'core/queue.dart';
import 'core/session.dart';
import 'core/theme.dart';
import 'features/auth/role_gate.dart';

class JbsApp extends StatefulWidget {
  const JbsApp({
    super.key,
    required this.api,
    required this.auth,
    required this.queue,
    required this.clock,
    required this.notifications,
  });

  final ApiClient api;
  final AuthStore auth;
  final ActionQueue queue;
  final ServerClock clock;
  final NotificationService notifications;

  @override
  State<JbsApp> createState() => _JbsAppState();
}

class _JbsAppState extends State<JbsApp> {
  late final AppSession _session;

  @override
  void initState() {
    super.initState();
    _session = AppSession();
    _session.bootstrap(auth: widget.auth, api: widget.api);
  }

  @override
  void dispose() {
    _session.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: '迷雾运营台',
      debugShowCheckedModeBanner: false,
      theme: buildAppTheme(),
      home: RoleGate(
        session: _session,
        api: widget.api,
        auth: widget.auth,
        queue: widget.queue,
        clock: widget.clock,
        notifications: widget.notifications,
      ),
    );
  }
}
