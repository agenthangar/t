import AppKit
import SwiftUI

@main
@MainActor
enum RnnrApplication {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        withExtendedLifetime(delegate) { app.run() }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    private var item: NSStatusItem?
    private let popover = NSPopover()
    private let model = SessionModel()

    func applicationDidFinishLaunching(_ notification: Notification) {
        let status = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item = status
        if let button = status.button {
            button.image = foxImage()
            button.image?.size = NSSize(width: 19, height: 19)
            if button.image == nil { button.title = "r" }
            button.toolTip = "rnnr — Running sessions"
            button.setAccessibilityLabel("rnnr running sessions")
            button.target = self
            button.action = #selector(togglePopover)
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        popover.behavior = .transient
        popover.animates = false
        popover.delegate = self
        model.dismiss = { [weak self] in self?.popover.performClose(nil) }
    }

    @objc private func togglePopover() {
        guard let button = item?.button else { return }
        if NSApp.currentEvent?.type == .rightMouseUp {
            popover.performClose(nil)
            let menu = NSMenu()
            let quit = menu.addItem(withTitle: "Quit rnnr", action: #selector(quit), keyEquivalent: "q")
            quit.target = self
            item?.menu = menu
            button.performClick(nil)
            item?.menu = nil
            return
        }
        if popover.isShown {
            popover.performClose(nil)
        } else {
            let screenHeight = button.window?.screen?.visibleFrame.height ?? 600
            let height = min(560, max(220, screenHeight - 24))
            popover.contentViewController = NSHostingController(rootView: SessionPopover(model: model, height: height))
            popover.contentSize = NSSize(width: 286, height: height)
            NSApp.activate(ignoringOtherApps: true)
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            popover.contentViewController?.view.window?.makeKey()
            model.opened()
        }
    }

    func popoverDidClose(_ notification: Notification) { model.closed() }
    @objc private func quit() { NSApp.terminate(nil) }
}
