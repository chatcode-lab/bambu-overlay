// Menu-bar launcher for the overlay server.
//
// Why it exists: macOS (15+) only lets a program reach the local network (the printer) if an *app* can be
// asked for permission. A server started from a background terminal/tmux has no app to ask, so macOS
// silently refuses with "No route to host". Running the server as a child of this app makes macOS show
// "Bambu Overlay would like to find devices on your local network" once, and the permission sticks.
//
// Build: tools/build-app.sh
import Cocoa
import ServiceManagement

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private let statusLine = NSMenuItem(title: "Starting…", action: nil, keyEquivalent: "")
    private let loginItem = NSMenuItem(title: "Start at Login", action: nil, keyEquivalent: "")
    private var server: Process?
    private var quitting = false
    private let projectDir: String
    private let python: String
    private let port: String

    override init() {
        let info = Bundle.main.infoDictionary ?? [:]
        projectDir = info["BOProjectDir"] as? String ?? FileManager.default.currentDirectoryPath
        python = info["BOPython"] as? String ?? "/usr/bin/python3"
        port = info["BOPort"] as? String ?? "8787"
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "printer.fill", accessibilityDescription: "Bambu Overlay")
        let menu = NSMenu()
        statusLine.isEnabled = false
        menu.addItem(statusLine)
        menu.addItem(.separator())
        menu.addItem(item("Open Control Panel", #selector(openControl), "c"))
        menu.addItem(item("Open Overlay Preview", #selector(openPreview), "p"))
        menu.addItem(item("Show Log", #selector(showLog), "l"))
        menu.addItem(item("Restart Server", #selector(restartServer), "r"))
        menu.addItem(.separator())
        loginItem.target = self
        loginItem.action = #selector(toggleLogin)
        menu.addItem(loginItem)
        menu.addItem(item("Quit Bambu Overlay", #selector(quit), "q"))
        statusItem.menu = menu
        refreshLoginItem()
        startServer()
        Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.poll() }
    }

    private func item(_ title: String, _ action: Selector, _ key: String) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
        i.target = self
        return i
    }

    // MARK: server process

    private var logURL: URL { URL(fileURLWithPath: projectDir).appendingPathComponent("cache/server.log") }

    private func startServer() {
        try? FileManager.default.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        let log = try? FileHandle(forWritingTo: logURL)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-u", "server.py"]
        p.currentDirectoryURL = URL(fileURLWithPath: projectDir)
        p.standardOutput = log
        p.standardError = log
        // Not "background": keeps macOS from stretching the server's timers (smooth OBS crop animation).
        p.qualityOfService = .userInitiated
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async { self?.serverExited(proc.terminationStatus) }
        }
        do {
            try p.run()
            server = p
        } catch {
            statusLine.title = "Could not start server: \(error.localizedDescription)"
        }
    }

    private func serverExited(_ code: Int32) {
        server = nil
        guard !quitting else { return }
        statusLine.title = "Server stopped (exit \(code)), restarting…"
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
            guard let self, self.server == nil, !self.quitting else { return }
            self.startServer()
        }
    }

    @objc private func restartServer() { server?.terminate() }  // terminationHandler restarts it

    @objc private func quit() {
        quitting = true
        server?.terminate()
        server?.waitUntilExit()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        server?.terminate()
    }

    // MARK: menu actions

    @objc private func openControl() { open("/control") }
    @objc private func openPreview() { open("/preview") }
    @objc private func showLog() { NSWorkspace.shared.open(logURL) }
    private func open(_ path: String) {
        if let url = URL(string: "http://127.0.0.1:\(port)\(path)") { NSWorkspace.shared.open(url) }
    }

    @objc private func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            else { try SMAppService.mainApp.register() }
        } catch {
            let alert = NSAlert()
            alert.messageText = "Could not change login item"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }
        refreshLoginItem()
    }

    private func refreshLoginItem() {
        loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off
    }

    // MARK: status line

    private func poll() {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/state") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            var text = "Server not responding"
            if let data, let v = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                let job = v["job"] as? [String: Any] ?? [:]
                if (v["online"] as? Bool) != true {
                    let printer = v["printer"] as? [String: Any] ?? [:]
                    text = "Printer offline (\(printer["status"] as? String ?? "?"))"
                } else if (job["hasJob"] as? Bool) == true {
                    text = "\(job["label"] as? String ?? "") · \(job["progress"] as? Int ?? 0)%"
                } else {
                    text = job["label"] as? String ?? "Idle"
                }
                if let obs = v["obs"] as? [String: Any], let s = obs["status"] as? String {
                    text += s.hasPrefix("connected") ? " · OBS connected" : " · OBS: \(s)"
                }
            }
            DispatchQueue.main.async { self?.statusLine.title = text }
        }.resume()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
