"""Keep QtQml runtime dependencies without bundling the full QML catalog.

QtWebEngineCore links QtQml, so the module and its normal shared-library
dependencies remain necessary. The stock PyInstaller hook additionally gathers
every installed QML plugin (3D, Charts, Graphs, controls, and more). This app
uses QWebEngineView with an HTML frontend and never loads QML content, so that
catalog adds substantial package size without providing a reachable feature.
"""

from PyInstaller.utils.hooks.qt import add_qt6_dependencies


hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
