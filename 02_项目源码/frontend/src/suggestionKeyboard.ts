/** Pure state machine: emits editing intents; never submits, reads a clipboard or prevents default. */
export type SuggestionContext = {
  answerId: string; branchId: string; contextRevision: string | number;
  sourceRevisions: Record<string, string>;
};
export type Suggestion = { id: string; text: string; context: SuggestionContext };
export type SuggestionPhase = 'hidden' | 'preparing' | 'ready' | 'accepted' | 'dismissed';
export type SuggestionState = {
  phase: SuggestionPhase; requestId: string | null; context: SuggestionContext; suggestion: Suggestion | null;
  focused: boolean; composing: boolean; visible: boolean; enabled: boolean; keyboardEnabled: boolean; draft: string;
  armed: { code: string; contextKey: string; suggestionId: string } | null;
  shiftCodes: string[]; blocked: boolean;
  acceptedEdit: { suggestionId: string; text: string; contextKey: string } | null;
};
export type KeyStroke = { key: string; code?: string; repeat?: boolean; shiftKey?: boolean;
  ctrlKey?: boolean; altKey?: boolean; metaKey?: boolean; isComposing?: boolean };
export type SuggestionEvent =
  | { type: 'context'; context: SuggestionContext }
  | { type: 'prepare'; requestId: string }
  | { type: 'receive'; suggestion: Suggestion; requestId: string }
  | { type: 'hide' | 'dismiss' | 'pointer' | 'selection' | 'undo' | 'accept' }
  | { type: 'focus'; focused: boolean }
  | { type: 'composition'; active: boolean }
  | { type: 'visibility'; visible: boolean }
  | { type: 'enabled'; enabled: boolean }
  | { type: 'keyboard-enabled'; enabled: boolean }
  | { type: 'input'; value: string }
  | ({ type: 'keydown' | 'keyup' } & KeyStroke);
export type SuggestionEffect = {
  type: 'fill' | 'clear'; text: string; selectionStart: number; selectionEnd: number;
  previousText: string; suggestionId: string; context: SuggestionContext;
  trigger?: 'keyboard' | 'explicit';
};
export type SuggestionTransition = { state: SuggestionState; effect?: SuggestionEffect };

export function suggestionContextKey(context: SuggestionContext): string {
  return JSON.stringify([context.answerId, context.branchId, String(context.contextRevision),
    Object.entries(context.sourceRevisions).sort(([a], [b]) => a.localeCompare(b))]);
}
const copyContext = (context: SuggestionContext): SuggestionContext => ({ ...context, sourceRevisions: { ...context.sourceRevisions } });
export function createSuggestionState(context: SuggestionContext, enabled = true, keyboardEnabled = true): SuggestionState {
  return { phase: 'hidden', requestId: null, context: copyContext(context), suggestion: null, focused: false, composing: false,
    visible: true, enabled, keyboardEnabled, draft: '', armed: null, shiftCodes: [], blocked: false, acceptedEdit: null };
}
export function suggestionVisible(state: SuggestionState): boolean {
  return state.enabled && state.phase === 'ready' && state.visible && !state.composing && state.draft === '' &&
    !!state.suggestion && suggestionContextKey(state.suggestion.context) === suggestionContextKey(state.context);
}
function accept(state: SuggestionState, trigger: 'keyboard' | 'explicit' = 'explicit'): SuggestionTransition {
  if (!suggestionVisible(state) || !state.suggestion) return { state: { ...state, armed: null } };
  const text = state.suggestion.text;
  return { state: { ...state, phase: 'accepted', draft: text, armed: null,
    acceptedEdit: { suggestionId: state.suggestion.id, text, contextKey: suggestionContextKey(state.context) } },
    effect: { type: 'fill', text, selectionStart: text.length, selectionEnd: text.length, previousText: '',
      suggestionId: state.suggestion.id, context: copyContext(state.context), trigger } };
}
/** All events operate on copies. An async response must carry the exact result context. */
export function reduceSuggestion(previous: SuggestionState, event: SuggestionEvent): SuggestionTransition {
  let state = { ...previous, shiftCodes: [...previous.shiftCodes] };
  const cancel = () => { state.armed = null; if (state.shiftCodes.length) state.blocked = true; };
  switch (event.type) {
    case 'context':
      if (suggestionContextKey(event.context) !== suggestionContextKey(state.context)) {
        state = { ...createSuggestionState(event.context, state.enabled, state.keyboardEnabled), focused: state.focused,
          composing: state.composing, visible: state.visible, draft: state.draft };
      }
      return { state };
    case 'prepare':
      if (state.phase !== 'dismissed' && state.phase !== 'accepted' && event.requestId) {
        state.phase = 'preparing'; state.requestId = event.requestId;
      }
      cancel(); return { state };
    case 'receive':
      if (!state.requestId || event.requestId !== state.requestId ||
          (state.phase !== 'preparing' && state.phase !== 'ready') ||
          suggestionContextKey(event.suggestion.context) !== suggestionContextKey(state.context) ||
          !event.suggestion.id || !event.suggestion.text.trim() || event.suggestion.text.length > 1000) return { state };
      state.suggestion = { ...event.suggestion, context: copyContext(event.suggestion.context) };
      state.phase = 'ready'; cancel(); return { state };
    case 'dismiss':
      state.phase = 'dismissed'; state.requestId = null; cancel(); return { state };
    case 'hide':
      state.phase = 'hidden'; state.requestId = null; state.suggestion = null; cancel(); return { state };
    case 'focus':
      state.focused = event.focused;
      if (!event.focused) { cancel(); state.shiftCodes = []; state.blocked = false; }
      return { state };
    case 'visibility':
      state.visible = event.visible;
      if (!event.visible) { cancel(); state.shiftCodes = []; state.blocked = false; }
      return { state };
    case 'composition': state.composing = event.active; cancel(); return { state };
    case 'enabled': state.enabled = event.enabled; cancel(); return { state };
    case 'keyboard-enabled': state.keyboardEnabled = event.enabled; cancel(); return { state };
    case 'input':
      if (event.value !== state.draft) state.acceptedEdit = null;
      state.draft = event.value; cancel(); return { state };
    case 'pointer': case 'selection': cancel(); return { state };
    case 'accept': return accept(state);
    case 'undo': {
      const edit = state.acceptedEdit;
      if (!edit || state.draft !== edit.text || edit.contextKey !== suggestionContextKey(state.context)) return { state };
      state = { ...state, phase: 'dismissed', draft: '', acceptedEdit: null, armed: null };
      return { state, effect: { type: 'clear', text: '', previousText: edit.text, selectionStart: 0,
        selectionEnd: 0, suggestionId: edit.suggestionId, context: copyContext(state.context) } };
    }
    case 'keydown': {
      if (event.key === 'Escape' && state.focused && !state.composing && !event.isComposing) {
        state.phase = 'dismissed'; state.requestId = null; cancel(); return { state };
      }
      if (event.key !== 'Shift') { cancel(); return { state }; }
      const code = event.code || '';
      if (state.shiftCodes.includes(code)) { cancel(); return { state }; }
      state.shiftCodes.push(code);
      if (!['ShiftLeft', 'ShiftRight'].includes(code) || event.repeat || state.shiftCodes.length !== 1 || state.blocked ||
          event.ctrlKey || event.altKey || event.metaKey || event.isComposing || !state.enabled || !state.keyboardEnabled ||
          !state.focused || !suggestionVisible(state)) { cancel(); return { state }; }
      state.armed = { code, contextKey: suggestionContextKey(state.context), suggestionId: state.suggestion!.id };
      return { state };
    }
    case 'keyup': {
      if (event.key !== 'Shift') { cancel(); return { state }; }
      const armed = state.armed;
      const code = event.code || '';
      const eligible = !!armed && armed.code === code && !state.blocked && state.shiftCodes.length === 1 &&
        !event.repeat && !event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey && !event.isComposing &&
        state.enabled && state.keyboardEnabled && state.focused && suggestionVisible(state) &&
        armed.contextKey === suggestionContextKey(state.context) && armed.suggestionId === state.suggestion?.id;
      state.shiftCodes = state.shiftCodes.filter((value) => value !== code);
      state.armed = null;
      if (!state.shiftCodes.length) state.blocked = false;
      else state.blocked = true;
      return eligible ? accept(state, 'keyboard') : { state };
    }
  }
}
