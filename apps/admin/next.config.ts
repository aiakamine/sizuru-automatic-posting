import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: {
    unoptimized: true,
  },
  turbopack: {
    // ホームディレクトリに無関係な package-lock.json があると、Turbopack が
    // モノレポルートをそこだと誤検出し「Next ignored ... outside the repo」の警告が出る。
    // 明示的にこのアプリのディレクトリをルートに固定して黙らせる（これ単体では
    // 下記の next/font/google ビルドエラーは解消しない。package.json の build
    // スクリプトのコメントを参照）。
    root: import.meta.dirname,
  },
};

export default nextConfig;
