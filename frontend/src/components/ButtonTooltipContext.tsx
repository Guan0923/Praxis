import { createContext, useContext } from "react";

export const ButtonTooltipContext = createContext(true);

export function useButtonTooltips() {
  return useContext(ButtonTooltipContext);
}
