// A short message at the top of the page, such as "Saved." or what went wrong.

export const notice = $state({ text: "", error: false });

export function say(text: string, error = false) {
  Object.assign(notice, { text, error });
  setTimeout(() => notice.text === text && (notice.text = ""), error ? 6000 : 3500);
}
