import { BrowserRouter } from "react-router-dom";
import AppRoutes from "./app/routes";
import { AppearanceProvider } from "./app/AppearanceProvider";

export { countUnreadArchived, loadArchiveReadState, markArchivedAsRead } from "./app/storage";

export default function App() {
  return <AppearanceProvider><BrowserRouter><AppRoutes /></BrowserRouter></AppearanceProvider>;
}
