/** Pure state machine: emits editing intents; never submits, reads a clipboard or prevents default. */
export type SuggestionContext = {
  answerId: string; branchId: string; contextRevision: string | number;
  sourceRevisions: Record<string, string>;
};
export type Suggestion = { id: string; text: string; context: SuggestionContext };
export type SuggestionPhase = 'hidden' | 'preparing' | 'ready' | 'accepted' | 'dismissed';
export type SuggestionState = {
  phase: SuggestionPhase; requestId: string | null; context: SuggestionContext; suggestion: Suggestion | null;
  focused: boolean; composing: boolean; visible: boolean; enabled: boolean; draft: string;
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
  | { type: 'input'; value: string }
  | ({ type: 'keydown' | 'keyup' } & KeyStroke);
export type SuggestionEffect = {
  type: 'fill' | 'clear'; text: string; selectionStart: number; selectionEnd: number;
  previousText: string; suggestionId: string; context: SuggestionContext;
};
export type SuggestionTransition = { state: SuggestionState; effect?: SuggestionEffect };

export function suggestionContextKey(context: SuggestionContext): string {
  return JSON.stringify([context.answerId, context.branchId, String(context.contextRevision),
    Object.entries(context.sourceRevisions).sort(([a], [b]) => a.localeCompare(b))]);
}
const copyContext = (context: SuggestionContext): SuggestionContext => ({ ...context, sourceRevisions: { ...context.sourceRevisions } });
export function createSuggestionState(context: SuggestionContext, enabled = true): SuggestionState {
  return { phase: 'hidden', requestId: null, context: copyContext(context), suggestion: null, focused: false, composing: false,
    visible: true, enabled, draft: '', acceptedEdit: null };
}
export function suggestionVisible(state: SuggestionState): boolean {
  return state.enabled && state.phase === 'ready' && state.visible && !state.composing && state.draft === '' &&
    !!state.suggestion && suggestionContextKey(state.suggestion.context) === suggestionContextKey(state.context);
}
function accept(state: SuggestionState): SuggestionTransition {
  if (!suggestionVisible(state) || !state.suggestion) return { state };
  const text = state.suggestion.text;
  return { state: { ...state, phase: 'accepted', draft: text,
    acceptedEdit: { suggestionId: state.suggestion.id, text, contextKey: suggestionContextKey(state.context) } },
    effect: { type: 'fill', text, selectionStart: text.length, selectionEnd: text.length, previousText: '',
      suggestionId: state.suggestion.id, context: copyContext(state.context) } };
}
/** All events operate on copies. An async response must carry the exact result context. */
export function reduceSuggestion(previous: SuggestionState, event: SuggestionEvent): SuggestionTransition {
  let state = { ...previous };
  switch (event.type) {
    case 'context':
      if (suggestionContextKey(event.context) !== suggestionContextKey(state.context)) {
        state = { ...createSuggestionState(event.context, state.enabled), focused: state.focused,
          composing: state.composing, visible: state.visible, draft: state.draft };
      }
      return { state };
    case 'prepare':
      if (state.phase !== 'dismissed' && state.phase !== 'accepted' && event.requestId) {
        state.phase = 'preparing'; state.requestId = event.requestId;
      }
      return { state };
    case 'receive':
      if (!state.requestId || event.requestId !== state.requestId ||
          (state.phase !== 'preparing' && state.phase !== 'ready') ||
          suggestionContextKey(event.suggestion.context) !== suggestionContextKey(state.context) ||
          !event.suggestion.id || !event.suggestion.text.trim() || event.suggestion.text.length > 1000) return { state };
      state.suggestion = { ...event.suggestion, context: copyContext(event.suggestion.context) };
      state.phase = 'ready'; return { state };
    case 'dismiss':
      state.phase = 'dismissed'; state.requestId = null; return { state };
    case 'hide':
      state.phase = 'hidden'; state.requestId = null; state.suggestion = null; return { state };
    case 'focus':
      state.focused = event.focused;
      
      return { state };
    case 'visibility':
      state.visible = event.visible;
      
      return { state };
    case 'composition': state.composing = event.active; return { state };
    case 'enabled': state.enabled = event.enabled; return { state };
    case 'input':
      if (event.value !== state.draft) state.acceptedEdit = null;
      state.draft = event.value; return { state };
    case 'pointer': case 'selection': return { state };
    case 'accept': return accept(state);
    case 'undo': {
      const edit = state.acceptedEdit;
      if (!edit || state.draft !== edit.text || edit.contextKey !== suggestionContextKey(state.context)) return { state };
      state = { ...state, phase: 'dismissed', draft: '', acceptedEdit: null };
      return { state, effect: { type: 'clear', text: '', previousText: edit.text, selectionStart: 0,
        selectionEnd: 0, suggestionId: edit.suggestionId, context: copyContext(state.context) } };
    }
    case 'keydown': {
      if (event.key === 'Escape' && state.focused && !state.composing && !event.isComposing) {
        state.phase = 'dismissed'; state.requestId = null; return { state };
      }
      return { state };
    }
    case 'keyup': return { state };
  }
}
