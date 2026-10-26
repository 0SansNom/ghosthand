import XCTest

/// The runner sends the raw tree; the host does the filtering and indexing.
func serialize(_ snapshot: XCUIElementSnapshot, depth: Int, maxDepth: Int) -> [String: Any] {
    let frame = snapshot.frame
    var node: [String: Any] = [
        "type": typeName(snapshot.elementType),
        "frame": [
            "x": frame.origin.x.isFinite ? frame.origin.x : 0,
            "y": frame.origin.y.isFinite ? frame.origin.y : 0,
            "width": frame.size.width.isFinite ? frame.size.width : 0,
            "height": frame.size.height.isFinite ? frame.size.height : 0,
        ],
        "enabled": snapshot.isEnabled,
        "selected": snapshot.isSelected,
        "focused": snapshot.hasFocus,
    ]
    if !snapshot.identifier.isEmpty { node["identifier"] = snapshot.identifier }
    if !snapshot.label.isEmpty { node["label"] = snapshot.label }
    if !snapshot.title.isEmpty { node["title"] = snapshot.title }
    if let placeholder = snapshot.placeholderValue, !placeholder.isEmpty { node["placeholder"] = placeholder }
    if let value = snapshot.value {
        let text = "\(value)"
        if !text.isEmpty { node["value"] = text }
    }
    if depth < maxDepth, !snapshot.children.isEmpty {
        node["children"] = snapshot.children.map { serialize($0, depth: depth + 1, maxDepth: maxDepth) }
    }
    return node
}

func typeName(_ type: XCUIElement.ElementType) -> String {
    switch type {
    case .application: return "Application"
    case .window: return "Window"
    case .alert: return "Alert"
    case .sheet: return "Sheet"
    case .button: return "Button"
    case .link: return "Link"
    case .staticText: return "StaticText"
    case .textField: return "TextField"
    case .secureTextField: return "SecureTextField"
    case .textView: return "TextView"
    case .searchField: return "SearchField"
    case .image: return "Image"
    case .icon: return "Icon"
    case .cell: return "Cell"
    case .table: return "Table"
    case .collectionView: return "CollectionView"
    case .scrollView: return "ScrollView"
    case .navigationBar: return "NavigationBar"
    case .tabBar: return "TabBar"
    case .toolbar: return "Toolbar"
    case .statusBar: return "StatusBar"
    case .switch: return "Switch"
    case .toggle: return "Toggle"
    case .slider: return "Slider"
    case .stepper: return "Stepper"
    case .segmentedControl: return "SegmentedControl"
    case .picker: return "Picker"
    case .pickerWheel: return "PickerWheel"
    case .datePicker: return "DatePicker"
    case .pageIndicator: return "PageIndicator"
    case .progressIndicator: return "ProgressIndicator"
    case .activityIndicator: return "ActivityIndicator"
    case .keyboard: return "Keyboard"
    case .key: return "Key"
    case .menu: return "Menu"
    case .menuItem: return "MenuItem"
    case .map: return "Map"
    case .webView: return "WebView"
    case .group: return "Group"
    case .other: return "Other"
    default: return "Other(\(type.rawValue))"
    }
}

func stateName(_ state: XCUIApplication.State) -> String {
    switch state {
    case .notRunning: return "notRunning"
    case .runningBackgroundSuspended: return "suspended"
    case .runningBackground: return "background"
    case .runningForeground: return "foreground"
    default: return "unknown"
    }
}

func orientationName(_ orientation: UIDeviceOrientation) -> String {
    switch orientation {
    case .portrait: return "portrait"
    case .portraitUpsideDown: return "portraitUpsideDown"
    case .landscapeLeft: return "landscapeLeft"
    case .landscapeRight: return "landscapeRight"
    default: return "unknown"
    }
}
