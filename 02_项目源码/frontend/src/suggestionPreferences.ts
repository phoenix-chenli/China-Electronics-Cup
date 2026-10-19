import { preferenceStorage } from './browserStorage';

export const SUGGESTION_KEYBOARD_KEY = 'zhiheng-suggestion-keyboard-enabled-v1';
export function readSuggestionKeyboardEnabled(): boolean {
  return preferenceStorage.getItem(SUGGESTION_KEYBOARD_KEY) !== 'false';
}
export function saveSuggestionKeyboardEnabled(enabled: boolean): void {
  // resilientStorage keeps the preference for this session if disk is denied.
  preferenceStorage.setItem(SUGGESTION_KEYBOARD_KEY, String(enabled));
}
