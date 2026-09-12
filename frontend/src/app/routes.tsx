import AgentApp from "./AgentApp";

export default function AppRoutes() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    if (!["/", "/benchmark", "/trash"].includes(pathname) && !parseChatPath(pathname)) navigate("/", { replace: true });
  }, [pathname, navigate]);
  return <AgentApp />;
}
import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { parseChatPath } from "./conversationNavigation";
