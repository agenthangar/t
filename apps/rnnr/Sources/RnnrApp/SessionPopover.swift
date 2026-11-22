import AppKit
import SwiftUI
import RnnrCore

private struct Slate {
    let dark: Bool
    var surface: Color { color(dark ? 0x1c252e : 0xe1e8ee) }
    var raised: Color { color(dark ? 0x26323d : 0xcedbe5) }
    var line: Color { color(dark ? 0x3a4a57 : 0xaebdc9) }
    var ink: Color { color(dark ? 0xe7edf1 : 0x1e2a34) }
    var muted: Color { color(dark ? 0xaebbc5 : 0x4b5d6d) }
    var accent: Color { color(dark ? 0xf2a93b : 0xedac50) }
    var mark: Color { color(dark ? 0xf2a93b : 0x875015) }
    var danger: Color { color(dark ? 0xff9c9f : 0x962c32) }
    private func color(_ hex: UInt32) -> Color {
        Color(.sRGB, red: Double((hex >> 16) & 255) / 255,
              green: Double((hex >> 8) & 255) / 255, blue: Double(hex & 255) / 255, opacity: 1)
    }
}

struct SessionPopover: View {
    @ObservedObject var model: SessionModel
    let height: CGFloat
    @Environment(\.colorScheme) private var colorScheme
    private var palette: Slate { Slate(dark: colorScheme == .dark) }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                if let fox = foxImage() {
                    Image(nsImage: fox).renderingMode(.template).resizable()
                        .scaledToFit().foregroundColor(palette.mark).frame(width: 22, height: 22)
                        .accessibilityHidden(true)
                }
                Text("Running sessions").font(.system(size: 15, weight: .semibold)).lineLimit(1)
                Spacer(minLength: 0)
                Button { model.perform(.new) } label: {
                    Text("New ↗").font(.system(size: 12, weight: .semibold))
                        .padding(.horizontal, 9).padding(.vertical, 5)
                        .foregroundColor(Color(red: 23/255, green: 24/255, blue: 21/255))
                        .background(palette.accent, in: RoundedRectangle(cornerRadius: 6))
                }.buttonStyle(.plain).modifier(KeyboardFocus(palette: palette)).help("Start a new session in a new Terminal window")
            }.padding(.horizontal, 12).padding(.top, 14).padding(.bottom, 12)

            ScrollView(.vertical) {
                LazyVStack(alignment: .leading, spacing: 0) {
                    if let notice = model.notice {
                        Text(notice).font(.system(size: 11)).foregroundColor(palette.muted)
                            .padding(.horizontal, 10).padding(.vertical, 8)
                            .accessibilityLabel(notice)
                    }
                    if model.rows.isEmpty && model.notice == nil {
                        Text("No detached sessions.").font(.system(size: 13))
                            .foregroundColor(palette.muted).padding(.horizontal, 10).padding(.vertical, 20)
                    }
                    ForEach(model.rows) { row in
                        SessionEntry(row: row, palette: palette,
                                     open: { model.perform(.attach(row)) }, end: { model.confirmEnd(row) })
                    }
                }.padding(.horizontal, 6).padding(.bottom, 8)
            }.frame(maxWidth: .infinity, maxHeight: .infinity)

            VStack(alignment: .leading, spacing: 8) {
                Text("Opens in a new Terminal window").font(.system(size: 11)).foregroundColor(palette.muted)
                VStack(spacing: 6) {
                    HStack(spacing: 6) {
                        action("All sessions", .all)
                        action("Open", .open)
                    }
                    HStack(spacing: 6) {
                        action("Resume", .resume)
                        action("Move", .move)
                    }
                }
            }.padding(.horizontal, 12).padding(.top, 10).padding(.bottom, 12)
                .overlay(alignment: .top) { Rectangle().fill(palette.line).frame(height: 1) }
        }
        .foregroundColor(palette.ink).background(palette.surface)
        .frame(width: 286, height: height)
    }

    private func action(_ title: String, _ action: TerminalAction) -> some View {
        Button { model.perform(action) } label: {
            HStack(spacing: 6) {
                Text(title)
                Spacer(minLength: 0)
                Text("↗").foregroundColor(palette.muted)
            }.font(.system(size: 12)).padding(.horizontal, 9).frame(maxWidth: .infinity, minHeight: 35)
        }.buttonStyle(SlateButton(palette: palette, bordered: true))
            .modifier(KeyboardFocus(palette: palette))
            .help("\(title) in a new Terminal window")
    }
}

private struct SessionEntry: View {
    let row: SessionRow
    let palette: Slate
    let open: () -> Void
    let end: () -> Void
    @State private var hovering = false
    var body: some View {
        HStack(spacing: 2) {
            Button(action: open) {
                HStack(spacing: 8) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(row.title).font(.system(size: 13, weight: .medium))
                            .lineLimit(2).multilineTextAlignment(.leading)
                        Text([row.slot, row.agent, row.hostLabel].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                            .font(.system(size: 11)).foregroundColor(palette.muted)
                            .lineLimit(2).multilineTextAlignment(.leading)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                    Text("↗").font(.system(size: 12)).foregroundColor(palette.muted)
                }.padding(.leading, 9).padding(.trailing, 7).padding(.vertical, 10)
                    .contentShape(Rectangle())
            }.buttonStyle(SlateButton(palette: palette, bordered: false))
                .modifier(KeyboardFocus(palette: palette))
                .accessibilityLabel("Open \(row.title), \(row.slot), \(row.hostLabel)")
            Button(action: end) {
                Text("×").font(.system(size: 16)).frame(width: 24, height: 26)
            }.buttonStyle(.plain).modifier(KeyboardFocus(palette: palette)).foregroundColor(palette.muted)
                .accessibilityLabel("End \(row.slot) on \(row.hostLabel)")
                .help("End session; saved history and worktree are preserved")
        }.background(hovering ? palette.raised : Color.clear, in: RoundedRectangle(cornerRadius: 6))
            .onHover { hovering = $0 }.disabled(!row.canTarget)
    }
}

private struct SlateButton: ButtonStyle {
    let palette: Slate
    let bordered: Bool
    @State private var hovering = false
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background((configuration.isPressed || hovering) ? palette.raised : Color.clear, in: RoundedRectangle(cornerRadius: 6))
            .overlay { if bordered { RoundedRectangle(cornerRadius: 6).stroke(palette.line, lineWidth: 1) } }
            .contentShape(RoundedRectangle(cornerRadius: 6))
            .onHover { hovering = $0 }
    }
}

private struct KeyboardFocus: ViewModifier {
    let palette: Slate
    @FocusState private var focused: Bool
    func body(content: Content) -> some View {
        content.focused($focused)
            .overlay {
                if focused {
                    RoundedRectangle(cornerRadius: 6).stroke(palette.mark, lineWidth: 2)
                        .allowsHitTesting(false)
                }
            }
    }
}

func foxImage() -> NSImage? {
    guard let url = Bundle.main.url(forResource: "fox-31", withExtension: "png"),
          let image = NSImage(contentsOf: url) else { return nil }
    image.isTemplate = true
    return image
}
