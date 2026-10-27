import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// token 只进系统安全存储，不写日志、不拼进 URL。
class AuthStore {
  AuthStore({FlutterSecureStorage? storage})
      : _storage = storage ?? const FlutterSecureStorage();

  final FlutterSecureStorage _storage;

  static const _merchantToken = 'merchant_token';
  static const _merchantRequest = 'merchant_request_id';
  static const _merchantBrowser = 'merchant_browser_token';
  static const _merchantName = 'merchant_display_name';
  static const _dmToken = 'dm_token';
  static const _dmRunLabel = 'dm_run_label';

  Future<void> saveMerchantRequest({
    required String requestId,
    required String browserToken,
  }) async {
    await _storage.write(key: _merchantRequest, value: requestId);
    await _storage.write(key: _merchantBrowser, value: browserToken);
  }

  Future<(String, String)?> readMerchantRequest() async {
    final id = await _storage.read(key: _merchantRequest);
    final token = await _storage.read(key: _merchantBrowser);
    if (id == null || token == null || id.isEmpty || token.isEmpty) {
      return null;
    }
    return (id, token);
  }

  Future<void> saveMerchantToken(
    String token, {
    String? displayName,
  }) async {
    await _storage.write(key: _merchantToken, value: token);
    if (displayName != null && displayName.isNotEmpty) {
      await _storage.write(key: _merchantName, value: displayName);
    }
  }

  Future<String?> readMerchantToken() => _storage.read(key: _merchantToken);

  Future<String?> readMerchantName() => _storage.read(key: _merchantName);

  Future<void> saveDmToken(String token, {String? label}) async {
    await _storage.write(key: _dmToken, value: token);
    if (label != null) {
      await _storage.write(key: _dmRunLabel, value: label);
    }
  }

  Future<String?> readDmToken() => _storage.read(key: _dmToken);

  Future<String?> readDmLabel() => _storage.read(key: _dmRunLabel);

  Future<void> clearMerchant() async {
    await _storage.delete(key: _merchantToken);
    await _storage.delete(key: _merchantName);
  }

  Future<void> clearMerchantRequest() async {
    await _storage.delete(key: _merchantRequest);
    await _storage.delete(key: _merchantBrowser);
  }

  Future<void> clearDm() async {
    await _storage.delete(key: _dmToken);
    await _storage.delete(key: _dmRunLabel);
  }

  Future<void> clearAll() async {
    await clearMerchant();
    await clearMerchantRequest();
    await clearDm();
  }
}
