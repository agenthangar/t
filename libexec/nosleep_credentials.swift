// Private credential adapter. Listing returns Wi-Fi attributes only; secrets never leave this process.
import Foundation
import Security
import CoreWLAN

let service = "agenthangar.nosleep.hotspot"
func validSSID(_ text: String) -> Bool {
    return !text.isEmpty && text.utf8.count <= 32 && !text.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
}
func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value) {
        FileHandle.standardOutput.write(data)
    }
}
func query(_ ssid: String) -> [String: Any] {
    return [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: ssid]
}
func save(_ ssid: String, _ password: Data) -> OSStatus {
    guard !password.isEmpty, password.count <= 1024,
          !password.contains(0), !password.contains(10), !password.contains(13) else { return errSecParam }
    let matching = query(ssid)
    let status = SecItemUpdate(matching as CFDictionary, [kSecValueData as String: password] as CFDictionary)
    if status != errSecItemNotFound { return status }
    // Existing connect uses Apple's security tool. Trust only that tool and this helper.
    var security: SecTrustedApplication?
    var own: SecTrustedApplication?
    guard SecTrustedApplicationCreateFromPath("/usr/bin/security", &security) == errSecSuccess,
          SecTrustedApplicationCreateFromPath(nil, &own) == errSecSuccess,
          let security, let own else { return errSecAuthFailed }
    var access: SecAccess?
    let allowed = [security, own] as CFArray
    let accessStatus = SecAccessCreate("Nosleep hotspot" as CFString, allowed, &access)
    guard accessStatus == errSecSuccess, let access else { return accessStatus }
    var entry = matching
    entry[kSecValueData as String] = password
    entry[kSecAttrLabel as String] = "Nosleep hotspot: " + ssid
    entry[kSecAttrAccess as String] = access
    return SecItemAdd(entry as CFDictionary, nil)
}
func listNetworks() {
    // File-based keychain attributes are enough to enumerate names. No unlock/auth UI on browse.
    SecKeychainSetUserInteractionAllowed(false)
    var searchList: CFArray?
    var keychains: [SecKeychain] = []
    if SecKeychainCopySearchList(&searchList) == errSecSuccess,
       let found = searchList as? [SecKeychain] { keychains = found }
    var system: SecKeychain?
    if SecKeychainOpen("/Library/Keychains/System.keychain", &system) == errSecSuccess,
       let system { keychains.append(system) }
    var names: [String: String] = [:]
    var unavailable = false
    // AirPort metadata is intentionally scoped; never dump or enumerate unrelated credentials.
    for (attribute, value, source) in [(kSecAttrDescription, "AirPort network password", "system"),
                                       (kSecAttrService, service, "nosleep")] {
        var matching: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            attribute as String: value, kSecReturnAttributes as String: true,
            kSecMatchLimit as String: kSecMatchLimitAll]
        if !keychains.isEmpty { matching[kSecMatchSearchList as String] = keychains }
        var result: CFTypeRef?
        let status = SecItemCopyMatching(matching as CFDictionary, &result)
        if status == errSecItemNotFound { continue }
        if status != errSecSuccess { unavailable = true; continue }
        guard let items = result as? [[String: Any]] else { unavailable = true; continue }
        for item in items {
            if let ssid = item[kSecAttrAccount as String] as? String, validSSID(ssid) { names[ssid] = source }
        }
    }
    let current = CWWiFiClient.shared().interface()?.ssid() ?? ""
    emit(["state": unavailable ? "unavailable" : "ok",
          "networks": names.keys.sorted().map { ["ssid": $0, "source": names[$0]!] },
          "current": validSSID(current) && current.lowercased() != "<redacted>" ? current : ""])
}
let args = Array(CommandLine.arguments.dropFirst())
if args == ["list"] { listNetworks(); exit(0) }
guard args.count == 2, validSSID(args[1]) else { emit(["status": Int(errSecParam)]); exit(2) }
let ssid = args[1]
var status: OSStatus = errSecParam
switch args[0] {
case "store":
    let password = FileHandle.standardInput.readDataToEndOfFile()
    status = save(ssid, password)
case "reuse":
    var password: NSString?
    status = CWKeychainFindWiFiPassword(.user, Data(ssid.utf8), &password)
    if status == errSecItemNotFound { status = CWKeychainFindWiFiPassword(.system, Data(ssid.utf8), &password) }
    if status == errSecSuccess, let password { status = save(ssid, Data((password as String).utf8)) }
    else if status == errSecSuccess { status = errSecItemNotFound }
case "verify":
    var matching = query(ssid)
    matching[kSecReturnData as String] = true
    var result: CFTypeRef?
    status = SecItemCopyMatching(matching as CFDictionary, &result)
    if status == errSecSuccess && (result as? Data)?.isEmpty != false { status = errSecItemNotFound }
default: break
}
emit(["status": Int(status)])
exit(status == errSecSuccess ? 0 : 1)
