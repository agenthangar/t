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
final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate, NSWindowDelegate {
    private var item: NSStatusItem?
    private let popover = NSPopover()
    private let model = SessionModel()
    private var setupWindow: NSWindow?
    private var fallbackPanel: NSPanel?

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
        model.dismiss = { [weak self] in self?.closePane() }
        let launchEvent = NSAppleEventManager.shared().currentAppleEvent ?? (notification.userInfo?["NSAppleEvent"] as? NSAppleEventDescriptor)
        if launchEvent?.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem { return }
        // Wait for the status item to acquire its menu-bar window before anchoring.
        DispatchQueue.main.async { [weak self] in self?.showPane() }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showPane()
        return false
    }

    @objc private func togglePopover() {
        guard let button = item?.button else { return }
        if NSApp.currentEvent?.type == .rightMouseUp {
            closePane()
            let menu = NSMenu()
            menu.addItem(withTitle: "Set Up rnnr…", action: #selector(openSetup), keyEquivalent: "").target = self
            let quit = menu.addItem(withTitle: "Quit rnnr", action: #selector(quit), keyEquivalent: "q")
            quit.target = self
            item?.menu = menu
            button.performClick(nil)
            item?.menu = nil
            return
        }
        if popover.isShown || fallbackPanel?.isVisible == true {
            closePane()
        } else {
            showPane()
        }
    }

    private func closePane() {
        popover.performClose(nil)
        fallbackPanel?.close()
    }

    @objc private func openSetup() {
        closePane()
        NSApp.activate(ignoringOtherApps: true)
        if let setupWindow { setupWindow.makeKeyAndOrderFront(nil); return }
        let view = GuidedSetup(source: model.source) { [weak self] in
            UserDefaults.standard.set(true, forKey: "guidedSetupCompleted")
            self?.setupWindow?.close()
            self?.setupWindow = nil
            self?.showPane()
        }
        let window = NSWindow(contentViewController: NSHostingController(rootView: view))
        window.title = "Set Up rnnr"
        window.styleMask = [.titled, .closable]
        window.isReleasedWhenClosed = false
        setupWindow = window
        window.center()
        window.makeKeyAndOrderFront(nil)
    }

    private func showPane() {
        if !UserDefaults.standard.bool(forKey: "guidedSetupCompleted") { openSetup(); return }
        NSApp.activate(ignoringOtherApps: true)
        if let fallbackPanel, fallbackPanel.isVisible {
            fallbackPanel.makeKeyAndOrderFront(nil)
            fallbackPanel.orderFrontRegardless()
            return
        }
        guard let item, item.isVisible, let button = item.button,
              let anchor = button.window, anchor.isVisible else {
            showFallbackPanel()
            return
        }
        if !popover.isShown {
            let screenHeight = anchor.screen?.visibleFrame.height ?? 600
            let height = min(560, max(220, screenHeight - 24))
            popover.contentViewController = NSHostingController(rootView: SessionPopover(model: model, height: height))
            popover.contentSize = NSSize(width: 286, height: height)
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            guard popover.isShown else {
                showFallbackPanel()
                return
            }
            model.opened()
        }
        popover.contentViewController?.view.window?.makeKey()
    }

    // A crowded or hidden menu bar may not provide a usable popover anchor.
    // Present the same pane near the menu bar so opening the app still works.
    private func showFallbackPanel() {
        let area = (item?.button?.window?.screen ?? NSScreen.main)?.visibleFrame
            ?? NSRect(x: 0, y: 0, width: 800, height: 600)
        let height = min(560, max(220, area.height - 48))
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 286, height: height),
                            styleMask: [.titled, .closable, .utilityWindow],
                            backing: .buffered, defer: false)
        panel.title = "rnnr"
        panel.isReleasedWhenClosed = false
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = .moveToActiveSpace
        panel.delegate = self
        panel.contentViewController = NSHostingController(rootView: SessionPopover(model: model, height: height))
        panel.setFrameTopLeftPoint(NSPoint(x: area.maxX - panel.frame.width - 12, y: area.maxY - 8))
        fallbackPanel = panel
        panel.makeKeyAndOrderFront(nil)
        panel.orderFrontRegardless()
        model.opened()
    }

    func popoverDidClose(_ notification: Notification) { model.closed() }
    func windowWillClose(_ notification: Notification) {
        model.closed()
        fallbackPanel = nil
    }
    @objc private func quit() { NSApp.terminate(nil) }
}
