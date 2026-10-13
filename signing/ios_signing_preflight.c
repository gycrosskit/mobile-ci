#include <Security/Security.h>
#include <CommonCrypto/CommonDigest.h>
#include <stdio.h>
#include <string.h>

static int fail(const char *operation, long status) {
    fprintf(stderr, "iOS signing preflight failed: %s (OSStatus=%ld).\n", operation, status);
    return 1;
}

int main(int argc, char **argv) {
    if (argc != 3) return fail("expected keychain path and identity SHA-1", errSecParam);
    // 此设置只作用于当前进程；不能用它包住另一个 codesign 进程并声称已禁止弹窗。
    OSStatus status = SecKeychainSetUserInteractionAllowed(false);
    if (status != errSecSuccess) return fail("disable interaction", status);
    SecKeychainRef keychain = NULL;
    status = SecKeychainOpen(argv[1], &keychain);
    if (status != errSecSuccess) return fail("open temporary keychain", status);
    SecKeychainStatus state = 0;
    status = SecKeychainGetStatus(keychain, &state);
    if (status != errSecSuccess) return fail("read temporary keychain status", status);
    if (!(state & kSecUnlockStateStatus)) return fail("temporary keychain is locked", errSecInteractionNotAllowed);
    CFArrayRef searchList = CFArrayCreate(NULL, (const void **)&keychain, 1, &kCFTypeArrayCallBacks);
    const void *keys[] = { kSecClass, kSecMatchSearchList, kSecMatchLimit, kSecReturnRef };
    const void *values[] = { kSecClassIdentity, searchList, kSecMatchLimitAll, kCFBooleanTrue };
    CFDictionaryRef query = CFDictionaryCreate(NULL, keys, values, 4,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    CFArrayRef identities = NULL;
    status = SecItemCopyMatching(query, (CFTypeRef *)&identities);
    if (status != errSecSuccess) return fail("find identity in temporary keychain", status);
    for (CFIndex i = 0; i < CFArrayGetCount(identities); ++i) {
        SecIdentityRef identity = (SecIdentityRef)CFArrayGetValueAtIndex(identities, i);
        SecCertificateRef certificate = NULL;
        status = SecIdentityCopyCertificate(identity, &certificate);
        if (status != errSecSuccess) return fail("read certificate", status);
        CFDataRef certificateData = SecCertificateCopyData(certificate);
        unsigned char digest[CC_SHA1_DIGEST_LENGTH];
        CC_SHA1(CFDataGetBytePtr(certificateData), (CC_LONG)CFDataGetLength(certificateData), digest);
        char hash[CC_SHA1_DIGEST_LENGTH * 2 + 1];
        for (int j = 0; j < CC_SHA1_DIGEST_LENGTH; ++j) sprintf(hash + j * 2, "%02X", digest[j]);
        CFRelease(certificateData);
        CFRelease(certificate);
        if (strcmp(hash, argv[2]) != 0) continue;
        SecKeyRef privateKey = NULL;
        status = SecIdentityCopyPrivateKey(identity, &privateKey);
        if (status != errSecSuccess) return fail("access private key", status);
        SecKeyAlgorithm algorithm = kSecKeyAlgorithmRSASignatureMessagePKCS1v15SHA256;
        if (!SecKeyIsAlgorithmSupported(privateKey, kSecKeyOperationTypeSign, algorithm)) {
            algorithm = kSecKeyAlgorithmECDSASignatureMessageX962SHA256;
        }
        const unsigned char message[] = "jenkins-ios-signing-preflight";
        CFDataRef data = CFDataCreate(NULL, message, sizeof(message) - 1);
        CFErrorRef error = NULL;
        CFDataRef signature = SecKeyCreateSignature(privateKey, algorithm, data, &error);
        if (signature == NULL) return fail("sign challenge without interaction",
            error ? CFErrorGetCode(error) : errSecInternalComponent);
        SecKeyRef publicKey = SecKeyCopyPublicKey(privateKey);
        if (publicKey == NULL) return fail("read public key", errSecInternalComponent);
        if (!SecKeyVerifySignature(publicKey, algorithm, data, signature, &error)) {
            return fail("verify challenge", error ? CFErrorGetCode(error) : errSecInternalComponent);
        }
        puts("iOS signing private-key preflight passed (interaction disabled).");
        return 0;
    }
    return fail("selected signing identity is missing", errSecItemNotFound);
}
