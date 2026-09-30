import { create } from "zustand";

interface UserInputState {
  /** 输入框当前文本，唯一事实来源 */
  text: string;
  /** 用外部内容整体替换输入框文本 */
  setText: (text: string) => void;
  /** 在当前文本末尾追加 */
  appendText: (text: string) => void;
  /** 清空输入框 */
  clear: () => void;
}

export const useUserInputStore = create<UserInputState>((set) => ({
  text: "",
  setText: (text) => set({ text }),
  appendText: (text) => set((state) => ({ text: state.text + text })),
  clear: () => set({ text: "" }),
}));
