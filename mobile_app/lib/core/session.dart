import 'package:flutter/foundation.dart';

import 'api_client.dart';
import 'auth_store.dart';

enum AppRole { none, merchant, dm }

/// 角色与登录态。token 只在内存和安全存储中，不写日志。
class AppSession extends ChangeNotifier {
  AppRole role = AppRole.none;
  String? merchantName;
  String? dmLabel;
  bool ready = false;

  Future<void> bootstrap({
    required AuthStore auth,
    required ApiClient api,
  }) async {
    try {
      final merchantToken = await auth.readMerchantToken();
      final dmToken = await auth.readDmToken();
      if (merchantToken != null && merchantToken.isNotEmpty) {
        api.token = merchantToken;
        merchantName = await auth.readMerchantName();
        role = AppRole.merchant;
      } else if (dmToken != null && dmToken.isNotEmpty) {
        api.token = dmToken;
        dmLabel = await auth.readDmLabel();
        role = AppRole.dm;
      } else {
        role = AppRole.none;
      }
    } catch (_) {
      role = AppRole.none;
    }
    ready = true;
    notifyListeners();
  }

  Future<void> signInMerchant({
    required AuthStore auth,
    required ApiClient api,
    required String token,
    String? displayName,
  }) async {
    await auth.saveMerchantToken(token, displayName: displayName);
    api.token = token;
    merchantName = displayName;
    role = AppRole.merchant;
    notifyListeners();
  }

  Future<void> signOutMerchant({
    required AuthStore auth,
    required ApiClient api,
  }) async {
    await auth.clearMerchant();
    api.token = null;
    merchantName = null;
    role = AppRole.none;
    notifyListeners();
  }

  Future<void> signInDm({
    required AuthStore auth,
    required ApiClient api,
    required String token,
    String? label,
  }) async {
    await auth.saveDmToken(token, label: label);
    api.token = token;
    dmLabel = label;
    role = AppRole.dm;
    notifyListeners();
  }

  Future<void> signOutDm({
    required AuthStore auth,
    required ApiClient api,
  }) async {
    await auth.clearDm();
    api.token = null;
    dmLabel = null;
    role = AppRole.none;
    notifyListeners();
  }
}
