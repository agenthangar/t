// One-shot helper app: only explicit configuration requests may ask for permission.
import AppKit
import CoreLocation
import CoreWLAN
import Darwin

final class Observer: NSObject, NSApplicationDelegate, CLLocationManagerDelegate {
    let directory: URL
    let authorize: Bool
    let owner: pid_t
    let started = Date()
    let location = CLLocationManager()
    var scanning = false
    var scanStarted: Date?
    var requested = false
    var timer: Timer?

    init(directory: URL, request: [String: Any]) {
        self.directory = directory
        self.authorize = request["authorize"] as? Bool ?? false
        self.owner = pid_t(request["owner"] as? Int ?? 0)
        super.init()
    }

    func finish(_ result: [String: Any]) {
        if let data = try? JSONSerialization.data(withJSONObject: result) {
            try? data.write(to: directory.appendingPathComponent("response.json"), options: .atomic)
        }
        NSApp.terminate(nil)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        location.delegate = self
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [self] _ in
            if !FileManager.default.fileExists(atPath: directory.path) || owner <= 0 || kill(owner, 0) != 0 {
                NSApp.terminate(nil)
            } else if Date().timeIntervalSince(scanStarted ?? started) > (scanning ? 10 : (authorize ? 120 : 10)) {
                finish(["state": "unknown"])
            }
        }
        checkPermission()
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        checkPermission()
    }

    func checkPermission() {
        guard !scanning else { return }
        guard CLLocationManager.locationServicesEnabled() else {
            finish(["state": "disabled"]); return
        }
        switch location.authorizationStatus {
        case .notDetermined:
            if authorize {
                if !requested {
                    requested = true
                    NSApp.activate(ignoringOtherApps: true)
                    location.requestAlwaysAuthorization()
                }
            } else {
                finish(["state": "permission"])
            }
        case .denied, .restricted:
            finish(["state": "denied"])
        case .authorizedAlways, .authorizedWhenInUse:
            scan()
        @unknown default:
            finish(["state": "unknown"])
        }
    }

    func scan() {
        scanning = true
        scanStarted = Date()
        DispatchQueue.global(qos: .utility).async { [self] in
            var result: [String: Any] = ["state": "unknown"]
            if let interface = CWWiFiClient.shared().interface() {
                if !interface.powerOn() {
                    result = ["state": "off"]
                } else {
                    do {
                        let networks = try interface.scanForNetworks(withSSID: nil)
                        result = ["state": "ok", "current": interface.ssid() ?? "",
                                  "networks": networks.map { ["ssid": $0.ssid ?? "", "rssi": $0.rssiValue] as [String: Any] }]
                    } catch {
                        result = ["state": "unknown"]
                    }
                }
            } else {
                result = ["state": "missing"]
            }
            let response = result
            DispatchQueue.main.async { [self] in finish(response) }
        }
    }
}

guard CommandLine.arguments.count == 2 else { exit(2) }
let directory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
guard let data = try? Data(contentsOf: directory.appendingPathComponent("request.json")),
      let request = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { exit(2) }
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let observer = Observer(directory: directory, request: request)
app.delegate = observer
app.run()
