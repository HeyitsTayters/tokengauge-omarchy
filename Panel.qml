import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons
import qs.Commons as Commons

// TokenGauge bar widget. All data comes from one long-lived
// `tokengauge.py --watch` process: it prints a snapshot as a JSON line every
// refresh and takes a line on stdin to refresh now, so its per-file log cache
// survives between refreshes. Settings live on the widget's shell.json entry.
Panel {
  id: root
  moduleName: "dev.tayters.tokengauge"
  ipcTarget: "dev.tayters.tokengauge"
  manageIpc: false

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  property var snapshot: ({})
  property bool backendDown: false
  property real nowMs: Date.now()
  // Open breakdowns by provider id, kept here because each snapshot rebuilds
  // the cards.
  property var expanded: ({})

  readonly property var providers: snapshot.providers || []
  readonly property bool loaded: snapshot.providers !== undefined
  readonly property string display: setting("display", "badges")
  readonly property real warnPercent: Number(setting("warnPercent", 80))

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color dimFg: Qt.darker(fg, 1.4)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color cardFill: Style.normalFillFor(fg, Color.accent, Color.urgent)
  readonly property color trackFill: Style.selectedFillFor(fg, Color.accent)

  readonly property string gaugeGlyph: String.fromCodePoint(0xf029a)    // md-gauge
  readonly property string refreshGlyph: String.fromCodePoint(0xf0450)  // md-refresh

  // ---- theme colors
  // The shell exposes red as `urgent` but not green or yellow, so take those
  // from the theme's terminal palette.

  property string themeText: ""

  function themeColor(key, fallback) {
    var match = themeText.match(new RegExp("^\\s*" + key + "\\s*=\\s*[\"']?(#[0-9A-Fa-f]{6})", "m"))
    return match ? match[1] : fallback
  }

  readonly property color okColor: themeColor("color2", "#4caf50")
  readonly property color cautionColor: themeColor("color3", "#e0b030")

  FileView {
    id: themeFile
    path: Color.currentThemePath + "/colors.toml"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.themeText = text()
  }

  // Theme switches arrive over IPC and repoint a symlink, which the file watch
  // can miss.
  Connections {
    target: Color
    function onAccentChanged() { themeFile.reload() }
    function onUrgentChanged() { themeFile.reload() }
  }

  function percentColor(percent) {
    if (percent >= warnPercent) return Color.urgent
    if (percent >= 50) return cautionColor
    return okColor
  }

  // ---- formatting

  function formatTokens(n) {
    var size = Math.abs(n)
    if (size >= 1e9) return Commons.I18n.formatNumber(n / 1e9, 1) + "B"
    if (size >= 1e6) return Commons.I18n.formatNumber(n / 1e6, 1) + "M"
    if (size >= 1e3) return Commons.I18n.formatNumber(Math.round(n / 1e3), 0) + "K"
    return Commons.I18n.formatNumber(n, 0)
  }

  function formatPercent(percent) { return Commons.I18n.formatNumber(Math.round(percent), 0) }

  function formatRelative(epochSeconds) {
    var seconds = Math.floor(epochSeconds - nowMs / 1000)
    if (seconds <= 0) return "now"
    var hours = Math.floor(seconds / 3600)
    var minutes = Math.floor((seconds % 3600) / 60)
    if (hours >= 24) return Math.floor(hours / 24) + "d " + (hours % 24) + "h"
    return hours > 0 ? hours + "h " + minutes + "m" : minutes + "m"
  }

  function formatClock(epochSeconds) {
    return new Date(epochSeconds * 1000).toLocaleTimeString(Qt.locale(Commons.I18n.formatLocale), Locale.ShortFormat)
  }

  function weekdayLetter(isoDay) {
    var parts = isoDay.split("-")
    var day = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]))
    return Qt.locale(Commons.I18n.formatLocale).standaloneDayName(day.getDay(), Locale.NarrowFormat)
  }

  // ---- derived values

  function knownPercents(provider) {
    var list = []
    var limits = provider.limits || []
    for (var i = 0; i < limits.length; i++)
      if (typeof limits[i].percent === "number" && limits[i].percent >= 0) list.push(limits[i].percent)
    return list
  }

  // One badge per local tool with its session and weekly % (the first two
  // limit windows), or its current token count when no limits are known.
  readonly property var badges: {
    var list = []
    for (var i = 0; i < providers.length; i++) {
      var p = providers[i]
      if ((p.id !== "claudeCode" && p.id !== "codex") || p.error) continue
      var percents = knownPercents(p).slice(0, 2)
      var fallback = ""
      if (percents.length === 0) {
        var first = (p.limits || [])[0]
        fallback = first && first.detail ? first.detail.replace(" tokens", "") : formatTokens(p.today.billable)
      }
      list.push({ id: p.id, percents: percents, fallback: fallback })
    }
    return list
  }

  readonly property real highestPercent: {
    var highest = -1
    for (var i = 0; i < providers.length; i++)
      highest = Math.max.apply(null, [highest].concat(knownPercents(providers[i])))
    return highest
  }

  readonly property real tokensToday: {
    var total = 0
    for (var i = 0; i < providers.length; i++) total += providers[i].today ? providers[i].today.billable : 0
    return total
  }

  readonly property bool showBadges: display === "badges" && badges.length > 0

  readonly property string barText: {
    if (!loaded || display === "icon") return ""
    if (display === "tokens" || highestPercent < 0) return formatTokens(tokensToday)
    return formatPercent(highestPercent) + "%"
  }

  // Names and numbers only: Omarchy's tooltip isn't ours to make plain text,
  // so nothing that came from a log or a server goes in it.
  readonly property string tooltip: {
    var lines = ["TokenGauge"]
    for (var i = 0; i < providers.length; i++) {
      var p = providers[i]
      if (p.error || (p.id !== "claudeCode" && p.id !== "codex" && p.id !== "anthropicAPI" && p.id !== "openAIAPI")) continue
      var percents = knownPercents(p).slice(0, 2).map(formatPercent)
      if (percents.length > 0) lines.push(strings.t("bar.limits").arg(displayName(p.id)).arg(percents.join(" / ") + "%"))
    }
    lines.push(strings.t("bar.tokensToday").arg(formatTokens(tokensToday)))
    return lines.join("\n")
  }

  function displayName(id) {
    if (id === "claudeCode") return "Claude Code"
    if (id === "codex") return "Codex"
    if (id === "anthropicAPI") return "Anthropic API"
    return "OpenAI API"
  }

  // ---- backend

  readonly property string scriptPath:
    decodeURIComponent(Qt.resolvedUrl("tokengauge.py").toString().replace(/^file:\/\//, ""))

  readonly property var backendCommand: {
    var command = ["python3", scriptPath, "--watch", "--interval", String(setting("refreshSeconds", 60))]
    if (!setting("claude", true)) command.push("--no-claude")
    if (!setting("codex", true)) command.push("--no-codex")
    if (!setting("accountLimits", true)) command.push("--no-account-limits")
    if (setting("anthropicApi", false)) command.push("--anthropic-api")
    if (setting("openaiApi", false)) command.push("--openai-api")
    command.push("--block-budget", String(Number(setting("blockBudget", 0)) || 0))
    command.push("--weekly-budget", String(Number(setting("weeklyBudget", 0)) || 0))
    return command
  }
  readonly property string backendKey: JSON.stringify(backendCommand)

  // Stopping it is enough: onExited brings it back with the new command.
  onBackendKeyChanged: if (watcher.running) watcher.running = false

  function refresh() { if (watcher.running) watcher.write("refresh\n") }

  onOpenedChanged: if (opened) refresh()

  Process {
    id: watcher
    command: root.backendCommand
    running: true
    stdinEnabled: true
    stdout: SplitParser {
      onRead: function(line) {
        try {
          root.snapshot = JSON.parse(line)
          root.backendDown = false
        } catch (e) {
          console.warn("tokengauge: bad snapshot line")
        }
      }
    }
    onExited: function(code) {
      root.backendDown = code !== 0
      retry.restart()
    }
  }

  Timer {
    id: retry
    interval: 5000
    onTriggered: watcher.running = true
  }

  // Reset countdowns tick between snapshots.
  Timer {
    interval: 30000
    running: true
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }

  ShellIpc {
    target: "dev.tayters.tokengauge"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): void { root.refresh() }
    function status(): string { return JSON.stringify(root.snapshot) }
  }

  // ---- bar button

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    dimmed: !root.loaded
    tooltipText: root.tooltip
    fixedWidth: vertical ? -1 : barContent.implicitWidth + scaledHorizontalMargin * 2
    fixedHeight: vertical ? barContent.implicitHeight + scaledVerticalPadding * 2 : -1
    onPressed: function(b) {
      if (b === Qt.RightButton) root.refresh()
      else root.toggle()
    }

    Grid {
      id: barContent
      anchors.centerIn: parent
      scale: button.pressScale
      // One row or one column. A fixed column count would leave spacing for
      // the empty columns, which reads as a gap beside the widget.
      columns: button.vertical ? 1 : -1
      rows: button.vertical ? -1 : 1
      columnSpacing: Style.space(9)
      rowSpacing: Style.space(6)
      horizontalItemAlignment: Grid.AlignHCenter
      verticalItemAlignment: Grid.AlignVCenter

      Repeater {
        model: root.showBadges ? root.badges : []

        Grid {
          id: badge
          required property var modelData
          columns: button.vertical ? 1 : -1
          rows: button.vertical ? -1 : 1
          columnSpacing: Style.space(3)
          rowSpacing: Style.space(2)
          horizontalItemAlignment: Grid.AlignHCenter
          verticalItemAlignment: Grid.AlignVCenter

          ToolGlyph {
            provider: badge.modelData.id
          }

          BarText {
            visible: badge.modelData.fallback !== ""
            text: badge.modelData.fallback
            color: root.fg
          }

          // Its own grid so the percents sit closer than the glyph does.
          Grid {
            columns: button.vertical ? 1 : 2
            rowSpacing: Style.space(2)
            horizontalItemAlignment: Grid.AlignHCenter

            Repeater {
              model: badge.modelData.percents

              Row {
                id: percentItem
                required property var modelData
                required property int index

                BarText {
                  visible: percentItem.index > 0 && !button.vertical
                  text: "/"
                  color: root.dimFg
                }

                BarText {
                  text: root.formatPercent(percentItem.modelData)
                    + (percentItem.index === badge.modelData.percents.length - 1 && !button.vertical ? "%" : "")
                  color: root.percentColor(percentItem.modelData)
                }
              }
            }
          }
        }
      }

      BarText {
        visible: !root.showBadges
        text: root.gaugeGlyph
        color: root.fg
        font.pixelSize: Style.bar.iconFont
      }

      BarText {
        visible: !root.showBadges && root.barText !== ""
        text: root.barText
        color: root.display !== "tokens" && root.highestPercent >= 0 ? root.percentColor(root.highestPercent) : root.fg
      }
    }
  }

  // ---- popup

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(panelColumn.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        var view = scrollArea.contentItem
        var limit = Math.max(0, view.contentHeight - view.height)
        view.contentY = Math.max(0, Math.min(limit, view.contentY + dy * Style.space(48)))
      }
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) { if (t === "r" || t === "R") root.refresh() }

      ScrollView {
        id: scrollArea
        anchors.fill: parent
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: panelColumn.implicitHeight > height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
        Binding {
          target: scrollArea.contentItem
          property: "interactive"
          value: panelColumn.implicitHeight > scrollArea.height
        }

        Column {
          id: panelColumn
          width: scrollArea.availableWidth
          spacing: Style.space(10)

          PanelHero {
            width: parent.width
            title: strings.t("panel.title")
            meta: !root.loaded ? strings.t("panel.loading")
              : strings.t("panel.todayUpdated").arg(root.formatTokens(root.tokensToday)).arg(root.formatClock(root.snapshot.updated))
            foreground: root.fg
            fontFamily: root.fontFamily
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: root.gaugeGlyph
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
            trailingControl: Component {
              PanelActionButton {
                iconText: root.refreshGlyph
                tooltipText: strings.t("panel.refresh")
                foreground: root.fg
                hoverColor: root.fg
                fontFamily: root.fontFamily
                onClicked: root.refresh()
              }
            }
          }

          PlainLabel {
            visible: root.backendDown || (root.loaded && root.providers.length === 0)
            width: parent.width
            wrapMode: Text.WordWrap
            text: root.backendDown ? strings.t("panel.backendDown") : strings.t("panel.noProviders")
            color: root.dimFg
          }

          Repeater {
            model: root.providers

            ProviderCard {
              required property var modelData
              width: panelColumn.width
              provider: modelData
            }
          }
        }
      }
    }
  }

  // ---- pieces

  component BarText: Text {
    textFormat: Text.PlainText
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    font.bold: true
    renderType: Text.NativeRendering
  }

  component PlainLabel: Text {
    textFormat: Text.PlainText
    color: root.fg
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
  }

  // Hand-drawn marks in the style of Claude and ChatGPT, tuned to stay legible
  // at bar size.
  component ToolGlyph: Canvas {
    id: glyph
    property string provider: ""
    property color tint: root.fg
    readonly property bool spark: provider === "claudeCode" || provider === "anthropicAPI"

    width: Style.space(16)
    height: width

    onTintChanged: requestPaint()
    onProviderChanged: requestPaint()
    onWidthChanged: requestPaint()

    onPaint: {
      var ctx = getContext("2d")
      ctx.reset()
      var r = Math.min(width, height) / 2
      ctx.translate(width / 2, height / 2)
      ctx.fillStyle = tint
      ctx.strokeStyle = tint
      if (spark) {
        // Rays of uneven length and spacing, narrow at the center and blunt at
        // the tip, so they stay separate instead of merging into a disc.
        var rays = [[-90, 0.98], [-58, 0.84], [-27, 0.96], [5, 0.82], [36, 0.98], [66, 0.86],
                    [97, 0.96], [128, 0.84], [158, 0.98], [190, 0.82], [221, 0.94], [250, 0.84]]
        var base = r * 0.035
        var tip = r * 0.085
        for (var i = 0; i < rays.length; i++) {
          var a = rays[i][0] * Math.PI / 180
          var ux = Math.cos(a)
          var uy = Math.sin(a)
          var l = r * rays[i][1] - tip
          ctx.beginPath()
          ctx.moveTo(-uy * base, ux * base)
          ctx.lineTo(ux * l - uy * tip, uy * l + ux * tip)
          ctx.arc(ux * l, uy * l, tip, a + Math.PI / 2, a - Math.PI / 2, true)
          ctx.lineTo(uy * base, -ux * base)
          ctx.closePath()
          ctx.fill()
        }
      } else {
        // Six rounded links, 60° apart and offset from center, interlock.
        ctx.lineWidth = r * 0.13
        for (var j = 0; j < 6; j++) {
          ctx.save()
          ctx.rotate(j * Math.PI / 3)
          ctx.translate(r * 0.30, 0)
          ctx.beginPath()
          ctx.roundedRect(-r * 0.23, -r * 0.90, r * 0.46, r * 1.0, r * 0.23, r * 0.23)
          ctx.stroke()
          ctx.restore()
        }
      }
    }
  }

  component LimitRow: Column {
    id: limitRow
    property var limit: ({})
    readonly property real percent: typeof limit.percent === "number" ? limit.percent : -1
    spacing: Style.space(3)

    Item {
      width: parent.width
      height: limitLabel.implicitHeight

      PlainLabel {
        id: limitLabel
        anchors.left: parent.left
        anchors.right: limitValues.left
        anchors.rightMargin: Style.space(8)
        elide: Text.ElideRight
        text: limitRow.limit.label || ""
      }

      Row {
        id: limitValues
        anchors.right: parent.right
        spacing: Style.space(6)

        PlainLabel {
          visible: limitRow.percent >= 0
          text: root.formatPercent(limitRow.percent) + "%"
          font.bold: true
        }

        PlainLabel {
          visible: !!limitRow.limit.detail
          text: limitRow.limit.detail || ""
          color: root.dimFg
        }

        PlainLabel {
          visible: !!limitRow.limit.resetsAt
          text: strings.t("card.resets").arg(root.formatRelative(limitRow.limit.resetsAt || 0))
          color: root.dimFg
        }
      }
    }

    Rectangle {
      visible: limitRow.percent >= 0
      width: parent.width
      height: Style.space(4)
      radius: height / 2
      color: root.trackFill

      Rectangle {
        width: parent.width * Math.max(0, Math.min(100, limitRow.percent)) / 100
        height: parent.height
        radius: parent.radius
        color: root.percentColor(limitRow.percent)
      }
    }
  }

  component DailyBars: Row {
    id: dailyBars
    property var days: []
    readonly property real peak: {
      var highest = 1
      for (var i = 0; i < days.length; i++) highest = Math.max(highest, days[i].tokens)
      return highest
    }
    spacing: Style.space(4)

    Repeater {
      model: dailyBars.days

      Column {
        id: dayColumn
        required property var modelData
        width: (dailyBars.width - dailyBars.spacing * 6) / 7
        spacing: Style.space(2)

        Item {
          width: parent.width
          height: Style.space(32)

          Rectangle {
            anchors.bottom: parent.bottom
            width: parent.width
            height: dayColumn.modelData.tokens > 0
              ? Math.max(Style.space(2), parent.height * dayColumn.modelData.tokens / dailyBars.peak) : 0
            radius: Math.min(Style.cornerRadius, Style.space(2))
            color: Color.accent
          }
        }

        PlainLabel {
          width: parent.width
          horizontalAlignment: Text.AlignHCenter
          text: root.weekdayLetter(dayColumn.modelData.day)
          color: root.dimFg
        }
      }
    }
  }

  component BreakdownRow: Row {
    id: breakdownRow
    property string label: ""
    property real today: 0
    property real week: 0

    PlainLabel {
      width: breakdownRow.width * 0.5
      text: breakdownRow.label
      color: root.dimFg
    }

    PlainLabel {
      width: breakdownRow.width * 0.25
      text: root.formatTokens(breakdownRow.today)
    }

    PlainLabel {
      width: breakdownRow.width * 0.25
      text: root.formatTokens(breakdownRow.week)
    }
  }

  component ProviderCard: Rectangle {
    id: card
    property var provider: ({})
    readonly property bool hasError: !!provider.error
    readonly property var today: provider.today || ({})
    readonly property var week: provider.week || ({})
    readonly property var days: provider.daily || []
    readonly property var models: (provider.topModels || []).slice(0, 3)
    readonly property bool showDaily: days.some(function(d) { return d.tokens > 0 })
    readonly property bool open: root.expanded[provider.id] === true

    implicitHeight: cardColumn.implicitHeight + Style.space(20)
    radius: Style.cornerRadius
    color: root.cardFill

    Column {
      id: cardColumn
      x: Style.space(10)
      y: Style.space(10)
      width: parent.width - Style.space(20)
      spacing: Style.space(8)

      Item {
        width: parent.width
        height: Math.max(cardTitle.implicitHeight, Style.space(14))

        ToolGlyph {
          id: cardGlyph
          anchors.verticalCenter: parent.verticalCenter
          provider: card.provider.id || ""
        }

        PlainLabel {
          id: cardTitle
          anchors.left: cardGlyph.right
          anchors.leftMargin: Style.space(6)
          anchors.verticalCenter: parent.verticalCenter
          text: root.displayName(card.provider.id)
          font.pixelSize: Style.font.body
          font.bold: true
        }

        PlainLabel {
          visible: !card.hasError
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          text: strings.t("card.totals").arg(root.formatTokens(card.today.billable || 0)).arg(root.formatTokens(card.week.billable || 0))
          color: root.dimFg
        }
      }

      PlainLabel {
        visible: card.hasError
        width: parent.width
        wrapMode: Text.WordWrap
        text: card.provider.error || ""
        color: Color.urgent
      }

      Repeater {
        model: card.hasError ? [] : (card.provider.limits || [])

        LimitRow {
          required property var modelData
          width: cardColumn.width
          limit: modelData
        }
      }

      DailyBars {
        visible: !card.hasError && card.showDaily
        width: parent.width
        days: card.days
      }

      Flow {
        visible: !card.hasError && card.models.length > 0
        width: parent.width
        spacing: Style.space(6)

        Repeater {
          model: card.models

          Rectangle {
            id: chip
            required property var modelData
            width: Math.min(chipLabel.implicitWidth + Style.space(10), cardColumn.width)
            height: chipLabel.implicitHeight + Style.space(4)
            radius: Math.min(Style.cornerRadius, height / 2)
            color: root.trackFill

            PlainLabel {
              id: chipLabel
              anchors.centerIn: parent
              width: Math.min(implicitWidth, cardColumn.width - Style.space(10))
              elide: Text.ElideRight
              text: String(chip.modelData.name).slice(0, 80) + " " + root.formatTokens(chip.modelData.tokens)
            }
          }
        }
      }

      Item {
        visible: !card.hasError
        width: parent.width
        height: breakdownToggle.implicitHeight

        PlainLabel {
          id: breakdownToggle
          text: (card.open ? "▾ " : "▸ ") + strings.t("card.breakdown")
          color: root.dimFg
        }

        MouseArea {
          anchors.fill: parent
          cursorShape: Qt.PointingHandCursor
          onClicked: {
            var next = Object.assign({}, root.expanded)
            next[card.provider.id] = !card.open
            root.expanded = next
          }
        }
      }

      Column {
        visible: !card.hasError && card.open
        width: parent.width
        spacing: Style.space(2)

        BreakdownRow { width: parent.width; label: strings.t("card.input"); today: card.today.input || 0; week: card.week.input || 0 }
        BreakdownRow { width: parent.width; label: strings.t("card.output"); today: card.today.output || 0; week: card.week.output || 0 }
        BreakdownRow { width: parent.width; label: strings.t("card.cacheWrite"); today: card.today.cacheWrite || 0; week: card.week.cacheWrite || 0 }
        BreakdownRow { width: parent.width; label: strings.t("card.cacheRead"); today: card.today.cacheRead || 0; week: card.week.cacheRead || 0 }
      }

      PlainLabel {
        visible: !!card.provider.note
        width: parent.width
        wrapMode: Text.WordWrap
        text: card.provider.note || ""
        color: root.dimFg
      }
    }
  }
}
