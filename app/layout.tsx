import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
export const metadata: Metadata = { title: "SwiftLabor.ai | Institutional Forex Intelligence", description: "MT5-first forex market intelligence, liquidity mapping and explainable trading research." };
export default function RootLayout({children}:{children:ReactNode}) { return <html lang="en"><body>{children}</body></html>; }

