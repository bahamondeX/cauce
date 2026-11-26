#[path = "../../../../src/cache/epoch.rs"]
mod epoch;

use epoch::Epoch;
use loom::{
    sync::{Arc, Mutex},
    thread,
};

#[test]
fn invalidation_and_retirement_fence_inflight_publication() {
    for retire in [false, true] {
        loom::model(move || {
            let state = Arc::new(Mutex::new((Epoch::default(), None)));
            let ticket = state.lock().unwrap().0.ticket();
            let publishing = state.clone();
            let fill = thread::spawn(move || {
                let mut state = publishing.lock().unwrap();
                if state.0.current(ticket) {
                    state.1 = Some("prior representation");
                }
            });
            let invalidating = state.clone();
            let invalidate = thread::spawn(move || {
                let mut state = invalidating.lock().unwrap();
                if retire {
                    state.0.retire();
                } else {
                    state.0.invalidate();
                }
                state.1.take();
            });
            fill.join().unwrap();
            invalidate.join().unwrap();
            let state = state.lock().unwrap();
            assert_eq!(state.0.retired(), retire);
            assert!(!state.0.current(ticket));
            assert_eq!(state.1, None);
        });
    }
}
