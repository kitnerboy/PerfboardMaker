"""
Left Toolbox panel showing categorized electronic components with drag-and-drop,
click-to-place, and debounced online Fritzing compatibility search.
"""
from __future__ import annotations

import os
import threading
from PyQt6.QtCore import Qt, QMimeData, QObject, QPoint, QRectF, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QDrag, QFont, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget
)

from app.components.fritzing_part import FritzingComponentDef
from app.components.registry import REGISTRY
from app.models.component_def import ComponentDef
from app.models.placed_component import PlacedComponent
from app.utils.fritzing_importer import import_fritzing_file, search_compatible_fritzing_parts


class _OnlineSearchSignals(QObject):
    found_part = pyqtSignal(int, object)  # (seq, FritzingComponentDef)
    finished = pyqtSignal(int, int, str)  # (seq, count, error_msg)


class ComponentCard(QFrame):
    clicked = pyqtSignal(str)  # type_id

    def __init__(self, comp_def: ComponentDef, is_online: bool = False, parent=None):
        super().__init__(parent)
        self.comp_def = comp_def
        self.is_online = is_online
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        if comp_def.description:
            self.setToolTip(comp_def.description)

        border_col = "#3d5a80" if is_online else "#383a40"
        self.setStyleSheet(f"""
            ComponentCard {{
                background-color: #2b2d31;
                border: 1px solid {border_col};
                border-radius: 6px;
                padding: 4px;
            }}
            ComponentCard:hover {{
                background-color: #35373c;
                border: 1px solid #5865f2;
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(10)

        # Generate thumbnail icon
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(48, 48)
        self.icon_label.setPixmap(self._render_thumbnail(48, 48))
        layout.addWidget(self.icon_label)

        # Text labels
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        info_layout.setContentsMargins(0, 0, 0, 0)

        title = QLabel(comp_def.name)
        title.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        title.setWordWrap(True)
        title.setStyleSheet("color: #f2f3f5;")
        info_layout.addWidget(title)

        off_count = len(getattr(comp_def, "off_grid_pins", []))
        pin_text = f"{len(comp_def.pins)}+{off_count} jumper pins" if off_count > 0 else f"{len(comp_def.pins)} pins"
        if is_online and isinstance(comp_def, FritzingComponentDef):
            src = comp_def.source_info.get("source_name", "Fritzing")
            sub_text = f"{pin_text} • {src} • Drag to save"
            sub_color = "#7aa2f7"
        else:
            sub_text = f"{pin_text} • {comp_def.default_label}"
            sub_color = "#949ba4"

        desc = QLabel(sub_text)
        desc.setFont(QFont("Arial", 8))
        desc.setStyleSheet(f"color: {sub_color};")
        info_layout.addWidget(desc)

        layout.addLayout(info_layout, 1)

        self._drag_start_pos = QPoint()

    def _render_thumbnail(self, w: int, h: int) -> QPixmap:
        pix = QPixmap(w, h)
        pix.fill(QColor(24, 25, 28))

        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        temp_inst = PlacedComponent(
            type_id=self.comp_def.type_id,
            grid_x=0, grid_y=0,
            label=self.comp_def.default_label
        )

        pitch = 14.0
        try:
            bounds = self.comp_def.get_shape(temp_inst, pitch).boundingRect()
        except Exception:
            bounds = QRectF()

        if bounds.isEmpty() or bounds.width() <= 0 or bounds.height() <= 0:
            bounds = QRectF(-pitch, -pitch, pitch * 2.0, pitch * 2.0)

        pad = 8.0
        scale = min((w - pad) / bounds.width(), (h - pad) / bounds.height(), 1.1)
        scale = max(scale, 0.05)

        painter.translate(w / 2.0, h / 2.0)
        painter.scale(scale, scale)
        painter.translate(-bounds.center().x(), -bounds.center().y())

        try:
            self.comp_def.draw_top(painter, temp_inst, pitch)
        except Exception:
            pass
        painter.end()
        return pix

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            if (event.pos() - self._drag_start_pos).manhattanLength() > 5:
                drag = QDrag(self)
                mime = QMimeData()
                mime.setData("application/x-perfboard-component", self.comp_def.type_id.encode("utf-8"))
                drag.setMimeData(mime)
                drag.setPixmap(self.icon_label.pixmap())
                drag.setHotSpot(QPoint(24, 24))
                drag.exec(Qt.DropAction.CopyAction)
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.comp_def.type_id)
        super().mouseReleaseEvent(event)


class ToolboxWidget(QWidget):
    component_selected = pyqtSignal(str)  # type_id to place

    _ONLINE_SEARCH_DELAY_MS = 2000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(260)
        self.setMaximumWidth(340)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # Header Title
        title_lbl = QLabel("COMPONENTS TOOLBOX")
        title_lbl.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        title_lbl.setStyleSheet("color: #dbdee1; letter-spacing: 0.5px;")
        main_layout.addWidget(title_lbl)

        # Search filter (local instant + 2s delayed online Fritzing search)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("🔍 Search local & online Fritzing...")
        self.search_box.textChanged.connect(self._on_search_text_changed)
        self.search_box.returnPressed.connect(self._on_search_return_pressed)
        main_layout.addWidget(self.search_box)

        self.import_btn = QPushButton("📂 Import Fritzing part from file...")
        self.import_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.import_btn.clicked.connect(self._on_import_file_clicked)
        main_layout.addWidget(self.import_btn)

        hint_lbl = QLabel("💡 Drag onto board or click to place. Online compatible parts appear 2s after pausing typing.")
        hint_lbl.setFont(QFont("Arial", 8))
        hint_lbl.setWordWrap(True)
        hint_lbl.setStyleSheet("color: #949ba4; padding-bottom: 4px;")
        main_layout.addWidget(hint_lbl)

        # Scroll Area for local + online component lists
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 4, 0)
        self.list_layout.setSpacing(10)

        # Local components section
        self.local_container = QWidget()
        self.local_layout = QVBoxLayout(self.local_container)
        self.local_layout.setContentsMargins(0, 0, 0, 0)
        self.local_layout.setSpacing(10)
        self.list_layout.addWidget(self.local_container)

        # Online Fritzing search results section (hidden until active search)
        self.online_section = QWidget()
        online_section_layout = QVBoxLayout(self.online_section)
        online_section_layout.setContentsMargins(0, 4, 0, 0)
        online_section_layout.setSpacing(8)

        self.online_header = QLabel("ONLINE FRITZING RESULTS")
        self.online_header.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        self.online_header.setStyleSheet("""
            color: #7aa2f7;
            border-bottom: 1px solid #383a40;
            padding-top: 6px;
            padding-bottom: 3px;
            margin-top: 4px;
        """)
        online_section_layout.addWidget(self.online_header)

        self.online_status_lbl = QLabel("")
        self.online_status_lbl.setFont(QFont("Arial", 8))
        self.online_status_lbl.setWordWrap(True)
        self.online_status_lbl.setStyleSheet("color: #949ba4;")
        online_section_layout.addWidget(self.online_status_lbl)

        self.online_cards_container = QWidget()
        self.online_cards_layout = QVBoxLayout(self.online_cards_container)
        self.online_cards_layout.setContentsMargins(0, 0, 0, 0)
        self.online_cards_layout.setSpacing(8)
        online_section_layout.addWidget(self.online_cards_container)

        self.online_section.setVisible(False)
        self.list_layout.addWidget(self.online_section)
        self.list_layout.addStretch()

        self.cards: list[tuple[ComponentDef, ComponentCard, QLabel | None]] = []
        self._online_cards: list[tuple[str, ComponentCard]] = []
        self._populate_components()

        self.scroll_area.setWidget(self.list_container)
        main_layout.addWidget(self.scroll_area)

        # Online search state & 2-second debounce timer
        self._search_seq: int = 0
        self._active_cancel_event: threading.Event | None = None
        self._online_signals = _OnlineSearchSignals(self)
        self._online_signals.found_part.connect(self._on_online_part_found)
        self._online_signals.finished.connect(self._on_online_search_finished)

        self._online_debounce = QTimer(self)
        self._online_debounce.setSingleShot(True)
        self._online_debounce.setInterval(self._ONLINE_SEARCH_DELAY_MS)
        self._online_debounce.timeout.connect(self._trigger_online_search)

        REGISTRY.add_install_listener(self._on_part_permanently_installed)

    def _clear_layout(self, layout: QVBoxLayout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _populate_components(self):
        self.cards.clear()
        self._clear_layout(self.local_layout)
        categories = REGISTRY.get_categories()

        for cat in categories:
            comps = REGISTRY.get_by_category(cat)
            if not comps:
                continue

            cat_header = QLabel(cat.upper())
            cat_header.setFont(QFont("Arial", 9, QFont.Weight.Bold))
            cat_header.setStyleSheet("""
                color: #5865f2;
                border-bottom: 1px solid #383a40;
                padding-top: 6px;
                padding-bottom: 3px;
                margin-top: 4px;
            """)
            self.local_layout.addWidget(cat_header)

            for comp in comps:
                card = ComponentCard(comp, is_online=False)
                card.clicked.connect(self.component_selected.emit)
                self.local_layout.addWidget(card)
                self.cards.append((comp, card, cat_header))

    def _on_part_permanently_installed(self, type_id: str):
        # Remove from temporary online results list and rebuild local categories
        remaining: list[tuple[str, ComponentCard]] = []
        for tid, card in self._online_cards:
            if tid == type_id:
                self.online_cards_layout.removeWidget(card)
                card.deleteLater()
            else:
                remaining.append((tid, card))
        self._online_cards = remaining

        self._populate_components()
        self._filter_components(self.search_box.text())

    def _on_import_file_clicked(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import Fritzing Part", "",
            "Fritzing Parts (*.fzpz *.fzp);;All Files (*)",
        )
        imported: list[str] = []
        failures: list[str] = []
        for path in paths:
            comp_def, reason = import_fritzing_file(path)
            name = os.path.basename(path)
            if comp_def is None:
                failures.append(f"{name}: {reason}")
            elif REGISTRY.is_installed(comp_def.type_id):
                failures.append(f"{name}: already imported")
            else:
                REGISTRY.register_preview(comp_def)
                REGISTRY.persist_if_remote(comp_def.type_id)
                imported.append(comp_def.name)

        if failures:
            QMessageBox.warning(self, "Import Fritzing Part", "\n".join(failures))
        elif imported:
            QMessageBox.information(
                self, "Import Fritzing Part", "Imported: " + ", ".join(imported)
            )

    def _on_search_text_changed(self, query: str):
        self._filter_components(query)
        self._cancel_online_search(hide_section=True)
        if len(query.strip()) >= 2:
            self._online_debounce.start()

    def _on_search_return_pressed(self):
        query = self.search_box.text().strip()
        if len(query) >= 2 and self._online_debounce.isActive():
            self._online_debounce.stop()
            self._trigger_online_search()

    def _cancel_online_search(self, hide_section: bool = True):
        self._online_debounce.stop()
        self._search_seq += 1
        if self._active_cancel_event is not None:
            self._active_cancel_event.set()
            self._active_cancel_event = None
        self._online_cards.clear()
        self._clear_layout(self.online_cards_layout)
        if hide_section:
            self.online_section.setVisible(False)
            self.online_status_lbl.setText("")

    def _trigger_online_search(self):
        query = self.search_box.text().strip()
        if len(query) < 2:
            return

        self._cancel_online_search(hide_section=False)
        seq = self._search_seq
        cancel_ev = threading.Event()
        self._active_cancel_event = cancel_ev

        self.online_section.setVisible(True)
        self.online_status_lbl.setText(f"🌐 Searching open-source Fritzing libraries for '{query}'...")

        exclude_ids = {c.type_id for c in REGISTRY.get_all()}

        def _worker():
            count = 0
            err_msg = ""
            try:
                def _on_match(comp_def: FritzingComponentDef):
                    nonlocal count
                    if cancel_ev.is_set():
                        return
                    count += 1
                    try:
                        self._online_signals.found_part.emit(seq, comp_def)
                    except RuntimeError:
                        pass

                search_compatible_fritzing_parts(
                    query,
                    exclude_type_ids=exclude_ids,
                    cancel_event=cancel_ev,
                    on_compatible_cb=_on_match,
                    limit_candidates=24,
                )
            except Exception as exc:
                err_msg = str(exc)

            if not cancel_ev.is_set():
                try:
                    self._online_signals.finished.emit(seq, count, err_msg)
                except RuntimeError:
                    pass

        threading.Thread(target=_worker, name="PerfBoardFritzingSearch", daemon=True).start()

    def _on_online_part_found(self, seq: int, comp_def: FritzingComponentDef):
        if seq != self._search_seq:
            return
        if not comp_def._get_renderer().isValid():
            return

        REGISTRY.register_preview(comp_def)
        card = ComponentCard(comp_def, is_online=True)
        card.clicked.connect(self.component_selected.emit)
        self.online_cards_layout.addWidget(card)
        self._online_cards.append((comp_def.type_id, card))

    def _on_online_search_finished(self, seq: int, count: int, err_msg: str):
        if seq != self._search_seq:
            return
        shown = len(self._online_cards)
        if err_msg:
            self.online_status_lbl.setText(f"⚠ Online search error: {err_msg}")
        elif shown == 0:
            self.online_status_lbl.setText("🌐 No compatible 2.54 mm online Fritzing parts found.")
        else:
            self.online_status_lbl.setText(
                f"🌐 {shown} compatible part(s) verified — drag onto board to save."
            )

    def _filter_components(self, query: str):
        query = query.strip().lower()
        visible_in_cat: dict[str, int] = {}

        for comp, card, cat_header in self.cards:
            matches = (
                query in comp.name.lower() or
                query in comp.category.lower() or
                query in comp.default_label.lower() or
                any(query in tag.lower() for tag in comp.tags)
            )
            card.setVisible(matches)
            if matches:
                visible_in_cat[comp.category] = visible_in_cat.get(comp.category, 0) + 1

        for comp, card, cat_header in self.cards:
            if cat_header:
                cat_header.setVisible(visible_in_cat.get(comp.category, 0) > 0)
