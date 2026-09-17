import type { Metadata } from "next";
import { Noto_Sans_JP, Roboto_Mono } from "next/font/google";
import { ConfigErrorScreen } from "@/components/ConfigErrorScreen";
import { getAppEnv } from "@/lib/env";
import { AppBootstrap } from "./AppBootstrap";
import "./globals.css";

const notoSansJp = Noto_Sans_JP({
  variable: "--font-noto-sans-jp",
  weight: ["400", "500", "700", "900"],
  subsets: ["latin"],
  display: "swap",
});

const robotoMono = Roboto_Mono({
  variable: "--font-roboto-mono",
  weight: ["500"],
  subsets: ["latin"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "Sizuru 管理画面",
  description: "投稿予約の週カレンダー管理画面",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  // `output: "export"` はビルド時の値がそのまま静的 HTML になるため、ここでの判定は
  // ビルドごとに一度だけ効く（実行時にブラウザの環境変数が変わることはない）。
  const configured = getAppEnv() !== null;

  return (
    <html lang="ja">
      <body className={`${notoSansJp.variable} ${robotoMono.variable}`}>
        {configured ? (
          <>
            <AppBootstrap />
            {children}
          </>
        ) : (
          <ConfigErrorScreen />
        )}
      </body>
    </html>
  );
}
