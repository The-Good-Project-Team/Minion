//! Bounded Accessibility-tree text sample for the focused app (macOS).
//!
//! Extracts titles and labels from the AX subtree under the focused window.
//! Requires Accessibility permission for Minion.

#[derive(Clone, Debug, serde::Serialize)]
pub struct AxNode {
    pub role: String,
    pub title: String,
    pub fingerprint: String,
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

#[cfg(target_os = "macos")]
mod macos {
    use accessibility::{
        AXUIElement, AXUIElementAttributes, TreeWalker, TreeVisitor, TreeWalkerFlow,
    };
    use core_foundation::string::CFString;
    use std::cell::{Cell, RefCell};
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};

    use super::AxNode;

    fn ax_text_from_root(root: &AXUIElement, max_chars: usize, max_depth: usize) -> Option<String> {
        let collector = AxTextCollector {
            buf: RefCell::new(String::new()),
            max_chars,
            depth: Cell::new(0),
            max_depth,
        };
        TreeWalker::new().walk(root, &collector);
        let s = collector.buf.borrow().trim().to_string();
        (!s.is_empty()).then_some(s)
    }

    fn title_matches(element: &AXUIElement, hint: &str) -> bool {
        let hint = hint.trim();
        if hint.is_empty() {
            return false;
        }
        let Ok(t) = element.title() else {
            return false;
        };
        let got = t.to_string();
        if got.eq_ignore_ascii_case(hint) {
            return true;
        }
        got.contains(hint) || hint.contains(got.as_str())
    }

    pub fn focused_window_ax_text(pid: i32, max_chars: usize, max_depth: usize) -> Option<String> {
        if pid <= 0 {
            return None;
        }
        let app = AXUIElement::application(pid);
        let root = app.focused_window().or_else(|_| app.main_window()).ok()?;
        ax_text_from_root(&root, max_chars, max_depth)
    }

    fn node_fingerprint(role: &str, title: &str) -> String {
        let piece: String = title.chars().take(80).collect();
        let raw = format!("{role}|{title}|{piece}");
        let mut h = DefaultHasher::new();
        raw.hash(&mut h);
        format!("{:016x}", h.finish())
    }

    fn element_bounds(_element: &AXUIElement) -> (f64, f64, f64, f64) {
        (0.0, 0.0, 0.0, 0.0)
    }

    fn walk_ax_nodes(
        root: &AXUIElement,
        max_nodes: usize,
        max_depth: usize,
    ) -> Vec<AxNode> {
        let collector = AxNodeCollector {
            nodes: RefCell::new(Vec::new()),
            max_nodes,
            depth: Cell::new(0),
            max_depth,
        };
        TreeWalker::new().walk(root, &collector);
        collector.nodes.into_inner()
    }

    pub fn collect_ax_nodes(
        pid: i32,
        title_hint: &str,
        max_nodes: usize,
        max_depth: usize,
    ) -> Vec<AxNode> {
        if pid <= 0 {
            return Vec::new();
        }
        let app = AXUIElement::application(pid);
        if let Ok(focused) = app.focused_window() {
            if title_matches(&focused, title_hint) {
                return walk_ax_nodes(&focused, max_nodes, max_depth);
            }
        }
        if let Ok(wins) = app.windows() {
            for w in wins.iter() {
                if title_matches(&w, title_hint) {
                    return walk_ax_nodes(&w, max_nodes, max_depth);
                }
            }
            if wins.len() == 1 {
                if let Some(w) = wins.get(0) {
                    return walk_ax_nodes(&w, max_nodes, max_depth);
                }
            }
        }
        if title_hint.trim().is_empty() {
            if let Ok(main) = app.main_window() {
                return walk_ax_nodes(&main, max_nodes, max_depth);
            }
        }
        Vec::new()
    }

    pub fn window_ax_text(
        pid: i32,
        _window_id: &str,
        title_hint: &str,
        max_chars: usize,
        max_depth: usize,
    ) -> Option<String> {
        if pid <= 0 {
            return None;
        }
        let app = AXUIElement::application(pid);
        if let Ok(focused) = app.focused_window() {
            if title_matches(&focused, title_hint) {
                return ax_text_from_root(&focused, max_chars, max_depth);
            }
        }
        if let Ok(wins) = app.windows() {
            for w in wins.iter() {
                if title_matches(&w, title_hint) {
                    return ax_text_from_root(&w, max_chars, max_depth);
                }
            }
            if wins.len() == 1 {
                if let Some(w) = wins.get(0) {
                    return ax_text_from_root(&w, max_chars, max_depth);
                }
            }
        }
        if title_hint.trim().is_empty() {
            if let Ok(main) = app.main_window() {
                return ax_text_from_root(&main, max_chars, max_depth);
            }
        }
        None
    }

    struct AxNodeCollector {
        nodes: RefCell<Vec<AxNode>>,
        max_nodes: usize,
        depth: Cell<usize>,
        max_depth: usize,
    }

    impl TreeVisitor for AxNodeCollector {
        fn enter_element(&self, element: &AXUIElement) -> TreeWalkerFlow {
            if self.nodes.borrow().len() >= self.max_nodes {
                return TreeWalkerFlow::Exit;
            }
            let d = self.depth.get();
            if d >= self.max_depth {
                return TreeWalkerFlow::SkipSubtree;
            }
            self.depth.set(d + 1);

            let role = element.role().map(|t| t.to_string()).unwrap_or_default();
            let mut title = element.title().map(|t| t.to_string()).unwrap_or_default();
            if title.is_empty() {
                if let Ok(t) = element.label_value() {
                    title = t.to_string();
                }
            }
            if !role.is_empty() || !title.is_empty() {
                let (x, y, w, h) = element_bounds(element);
                let fp = node_fingerprint(&role, &title);
                self.nodes.borrow_mut().push(AxNode {
                    role,
                    title,
                    fingerprint: fp,
                    x,
                    y,
                    w,
                    h,
                });
            }

            TreeWalkerFlow::Continue
        }

        fn exit_element(&self, _element: &AXUIElement) {
            self.depth.set(self.depth.get().saturating_sub(1));
        }
    }

    struct AxTextCollector {
        buf: RefCell<String>,
        max_chars: usize,
        depth: Cell<usize>,
        max_depth: usize,
    }

    impl AxTextCollector {
        fn push_line(&self, piece: &str) {
            let t = piece.trim();
            if t.is_empty() {
                return;
            }
            let mut b = self.buf.borrow_mut();
            if b.len() >= self.max_chars {
                return;
            }
            if !b.is_empty() {
                b.push('\n');
            }
            let room = self.max_chars.saturating_sub(b.len());
            if room == 0 {
                return;
            }
            let safe: String = t.chars().take(room).collect();
            b.push_str(&safe);
        }
    }

    impl TreeVisitor for AxTextCollector {
        fn enter_element(&self, element: &AXUIElement) -> TreeWalkerFlow {
            if self.buf.borrow().len() >= self.max_chars {
                return TreeWalkerFlow::Exit;
            }
            let d = self.depth.get();
            if d >= self.max_depth {
                return TreeWalkerFlow::SkipSubtree;
            }
            self.depth.set(d + 1);

            if let Ok(t) = element.title() {
                self.push_line(&t.to_string());
            }
            if let Ok(t) = element.label_value() {
                self.push_line(&t.to_string());
            }
            if let Ok(t) = element.description() {
                self.push_line(&t.to_string());
            }
            if let Ok(t) = element.value_description() {
                self.push_line(&t.to_string());
            }
            // AXValue carries the ACTUAL on-screen text for static-text / text-field /
            // text-area / heading / link elements — i.e. web page bodies, terminal
            // output, native content. Title/label/description only give chrome, so
            // without reading value() the walk captures the toolbar, not the page.
            if let Ok(v) = element.value() {
                if let Some(s) = v.downcast::<CFString>() {
                    self.push_line(&s.to_string());
                }
            }

            TreeWalkerFlow::Continue
        }

        fn exit_element(&self, _element: &AXUIElement) {
            self.depth.set(self.depth.get().saturating_sub(1));
        }
    }
}

#[cfg(target_os = "macos")]
pub fn focused_window_ax_text(pid: i32, max_chars: usize, max_depth: usize) -> Option<String> {
    macos::focused_window_ax_text(pid, max_chars, max_depth)
}

#[cfg(target_os = "macos")]
pub fn window_ax_text(
    pid: i32,
    window_id: &str,
    title_hint: &str,
    max_chars: usize,
    max_depth: usize,
) -> Option<String> {
    macos::window_ax_text(pid, window_id, title_hint, max_chars, max_depth)
}

#[cfg(not(target_os = "macos"))]
pub fn focused_window_ax_text(_pid: i32, _max_chars: usize, _max_depth: usize) -> Option<String> {
    None
}

#[cfg(not(target_os = "macos"))]
pub fn window_ax_text(
    _pid: i32,
    _window_id: &str,
    _title_hint: &str,
    _max_chars: usize,
    _max_depth: usize,
) -> Option<String> {
    None
}

#[cfg(target_os = "macos")]
pub fn collect_ax_nodes(
    pid: i32,
    title_hint: &str,
    max_nodes: usize,
    max_depth: usize,
) -> Vec<AxNode> {
    macos::collect_ax_nodes(pid, title_hint, max_nodes, max_depth)
}

#[cfg(not(target_os = "macos"))]
pub fn collect_ax_nodes(
    _pid: i32,
    _title_hint: &str,
    _max_nodes: usize,
    _max_depth: usize,
) -> Vec<AxNode> {
    Vec::new()
}
