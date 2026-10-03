import { NextResponse } from 'next/server';
import fs from 'fs';
import path from 'path';
import * as XLSX from 'xlsx';

const DATA_DIR = path.join(process.cwd(), '..', 'data', 'ads');

export async function GET(request, { params }) {
  try {
    const { id } = await params;

    const fileName = `${id}.json`;
    let runData = null;

    if (fs.existsSync(DATA_DIR)) {
      const categories = fs.readdirSync(DATA_DIR, { withFileTypes: true })
        .filter(d => d.isDirectory())
        .map(d => d.name);

      for (const cat of categories) {
        const filePath = path.join(DATA_DIR, cat, fileName);
        if (fs.existsSync(filePath)) {
          const content = fs.readFileSync(filePath, 'utf-8');
          runData = JSON.parse(content);
          break;
        }
      }
    }

    if (!runData || !runData.accepted) {
      return NextResponse.json({ error: 'Run not found or no accepted ads' }, { status: 404 });
    }

    const acceptedAds = runData.accepted;

    const rows = acceptedAds.map(ad => ({
      'Ad Link': ad.ad_archive_id ? `https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=ALL&view_all_page_id=${ad.advertiser_id || ''}&search_type=keyword_unordered&media_type=all&q=&ad_id=${ad.ad_archive_id}` : '',
      'CTA Text': ad.cta_text || '',
      'CTA URL': ad.cta_url || '',
      'Keyword': ad.source_keyword || '',
      'Advertiser': ad.advertiser_name || '',
      'Ad Text': ad.ad_text || '',
      'Confidence': ad.confidence || '',
      'Reason': ad.reason || '',
      'Fetched At': ad.fetched_at || '',
    }));

    const worksheet = XLSX.utils.json_to_sheet(rows);
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, worksheet, 'Accepted Ads');

    const buffer = XLSX.write(workbook, { type: 'buffer', bookType: 'xlsx' });

    return new NextResponse(buffer, {
      headers: {
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'Content-Disposition': `attachment; filename="run_${id}_export.xlsx"`,
      },
    });
  } catch (error) {
    console.error('Error generating Excel export:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}