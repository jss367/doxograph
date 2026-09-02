import AppKit

/// The menu bar. A web view in a window still needs Copy and Paste to work in
/// the review fields, and Cmd-Q to exist at all, so the app builds the standard
/// menus itself rather than shipping a nib.
extension AppDelegate {
    func buildMenu() -> NSMenu {
        let main = NSMenu()
        main.addItem(submenu("Doxograph", appMenu()))
        main.addItem(submenu("File", fileMenu()))
        main.addItem(submenu("Edit", editMenu()))
        main.addItem(submenu("View", viewMenu()))
        main.addItem(submenu("Window", windowMenu()))
        return main
    }

    private func submenu(_ title: String, _ menu: NSMenu) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        menu.title = title
        item.submenu = menu
        return item
    }

    private func item(
        _ title: String,
        _ action: Selector?,
        _ key: String = "",
        modifiers: NSEvent.ModifierFlags = .command,
        target: AnyObject? = nil
    ) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.keyEquivalentModifierMask = modifiers
        item.target = target
        return item
    }

    private func appMenu() -> NSMenu {
        let menu = NSMenu()
        menu.addItem(item("About Doxograph", #selector(NSApplication.orderFrontStandardAboutPanel(_:))))
        menu.addItem(item("Update Doxograph…", #selector(updateDoxograph(_:)), target: self))
        menu.addItem(.separator())
        menu.addItem(item("Hide Doxograph", #selector(NSApplication.hide(_:)), "h"))
        menu.addItem(item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h",
                          modifiers: [.command, .option]))
        menu.addItem(item("Show All", #selector(NSApplication.unhideAllApplications(_:))))
        menu.addItem(.separator())
        menu.addItem(item("Quit Doxograph", #selector(NSApplication.terminate(_:)), "q"))
        return menu
    }

    private func fileMenu() -> NSMenu {
        let menu = NSMenu()
        menu.addItem(item("Add Papers…", #selector(addPapers(_:)), "o", target: self))
        menu.addItem(.separator())
        menu.addItem(item("Close Window", #selector(NSWindow.performClose(_:)), "w"))
        return menu
    }

    private func editMenu() -> NSMenu {
        let menu = NSMenu()
        menu.addItem(item("Undo", Selector(("undo:")), "z"))
        menu.addItem(item("Redo", Selector(("redo:")), "z", modifiers: [.command, .shift]))
        menu.addItem(.separator())
        menu.addItem(item("Cut", #selector(NSText.cut(_:)), "x"))
        menu.addItem(item("Copy", #selector(NSText.copy(_:)), "c"))
        menu.addItem(item("Paste", #selector(NSText.paste(_:)), "v"))
        menu.addItem(item("Select All", #selector(NSText.selectAll(_:)), "a"))
        return menu
    }

    private func viewMenu() -> NSMenu {
        let menu = NSMenu()
        menu.addItem(item("Reload", #selector(reloadPage(_:)), "r", target: self))
        menu.addItem(item("Back", #selector(goBack(_:)), "[", target: self))
        menu.addItem(.separator())
        menu.addItem(item("Actual Size", #selector(actualSize(_:)), "0", target: self))
        menu.addItem(item("Zoom In", #selector(zoomIn(_:)), "+", target: self))
        menu.addItem(item("Zoom Out", #selector(zoomOut(_:)), "-", target: self))
        return menu
    }

    private func windowMenu() -> NSMenu {
        let menu = NSMenu()
        menu.addItem(item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"))
        menu.addItem(item("Zoom", #selector(NSWindow.performZoom(_:))))
        menu.addItem(.separator())
        menu.addItem(item("Bring All to Front", #selector(NSApplication.arrangeInFront(_:))))
        return menu
    }
}
