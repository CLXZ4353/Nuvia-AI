/** Minimal observable state container for the dependency-free frontend. */
export function createStore(initialState) {
  let state = Object.freeze({ ...initialState });
  const listeners = new Set();

  const publish = () => listeners.forEach((listener) => listener(state));
  return {
    get() { return state; },
    set(patch) {
      state = Object.freeze({ ...state, ...patch });
      publish();
      return state;
    },
    update(updater) {
      const patch = updater(state);
      return this.set(patch || {});
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

