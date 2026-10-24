import '@/app/globals.css';
import { Figtree, Newsreader } from 'next/font/google';
import { Nav } from '@/components/Nav';
import { NavigationProgress } from '@/components/NavigationProgress';

const sans = Figtree({ subsets: ['latin'], variable: '--font-sans', display: 'swap' });
const serif = Newsreader({ subsets: ['latin'], style: ['normal', 'italic'], variable: '--font-serif', display: 'swap' });

export const metadata = {
  title: 'Tesserae',
  description: 'Meta Ad Library analysis',
};

export default function RootLayout({ children }) {
  return (
    <html lang="en" className={`dark ${sans.variable} ${serif.variable}`}>
      <body className="bg-background font-sans text-foreground antialiased">
        <Nav />
        <NavigationProgress />
        <main>{children}</main>
      </body>
    </html>
  );
}
