"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useSession } from "@/hooks/useSession";

/** ログイン状態に応じて `/calendar/` または `/login/` へ振り分けるだけの入口。 */
export default function RootPage() {
  const router = useRouter();
  const session = useSession();

  useEffect(() => {
    if (session.status === "signed-in") {
      router.replace("/calendar/");
    } else if (session.status === "signed-out") {
      router.replace("/login/");
    }
  }, [session, router]);

  return null;
}
