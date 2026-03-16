---
id: workflow-03-visual-gen-images
type: workflow_step
name: Visual Generation - Images
model: claude
version: 1
---

## Instructions

Generate image creation prompts for a DIQIT content piece. You are a visual creative director producing detailed, production-ready image prompts.

<!-- CONTEXT -->
## Brand Visual Identity
- **Brand:** {brand_name}
- **Primary colour:** Black (#000000)
- **Accent colour:** Red-orange (#EF4324)
- **Image style:** Dark backgrounds with glowing tech elements, subtle blue or orange accent tones, professional data-driven aesthetic. Futuristic, enterprise-grade feel.
- **Typography:** Roboto (all weights)
- **Audience:** CxOs and VPs at multi-store F&B chains in APAC

## Content Input
- **Title:** {title}
- **Content type:** {content_type}
- **Content body:** {notes}

<!-- TASK -->
## Step 1 — Identify visual moments
Read the content draft. List the 2-5 key visual moments that support the narrative arc. For each moment, note the core concept it must communicate.

## Step 2 — Generate image prompts
For each visual moment, write a detailed image generation prompt covering:
- **Subject and composition:** What is shown, how it is framed
- **Style:** Dark tech aesthetic consistent with DIQIT brand identity
- **Colour palette:** Black base, red-orange (#EF4324) accents, subtle blue or warm orange glows
- **Mood and lighting:** Dramatic, professional, forward-looking
- **Text overlay:** Specify overlay text and placement, or state "none"

## Step 3 — Assign dimensions
Tag each prompt with the correct output dimensions based on content type:
| Content Type | Dimensions | Aspect Ratio |
|---|---|---|
| blog hero | 1200×630 | ~1.91:1 |
| linkedin_post | 1200×627 | ~1.91:1 |
| carousel slide | 1080×1080 | 1:1 |
| portrait / reel cover | 1080×1350 | 4:5 |
| story | 1080×1920 | 9:16 |

## Output Format
Return a JSON array. Each element represents one image prompt:
```json
[
  {
    "asset_id": "hero_01",
    "role": "Hero image | Supporting image | Thumbnail",
    "prompt": "Full image generation prompt text...",
    "dimensions": "1200x630",
    "aspect_ratio": "1.91:1",
    "text_overlay": "Optional overlay text or null",
    "notes": "Any production notes"
  }
]
```

## Example
```json
[
  {
    "asset_id": "hero_01",
    "role": "Hero image",
    "prompt": "A dark control-room dashboard glowing with orange and blue data visualizations. Multiple restaurant floor plans displayed on floating holographic screens. Black background, dramatic rim lighting in red-orange (#EF4324). No people. Clean, futuristic, enterprise-grade aesthetic. Roboto-style typography on screen elements.",
    "dimensions": "1200x630",
    "aspect_ratio": "1.91:1",
    "text_overlay": null,
    "notes": "Use as blog hero and LinkedIn preview image"
  }
]
```

<!-- Temperature note: Use temperature 0.8–1.0 for creative, varied image prompt generation. -->

<!-- NEGATIVE CONSTRAINTS -->
## Do NOT
- Use bright, flat, or cartoonish styles — DIQIT visuals are always dark and professional
- Include stock-photo-style images of people smiling at cameras
- Use white or light backgrounds unless explicitly requested
- Generate prompts for generic tech imagery — every image must connect to restaurant/F&B operations or data intelligence
- Add DIQIT logo to prompts — logos are composited separately in production

## Brief Template

CONTENT_ITEM: {title}
CONTENT_TYPE: {content_type}
BRAND: {brand_name}
CONTENT_BODY: {notes}
BRAND_COLOURS: reference brand guidelines
OUTPUT: Image generation prompts with specs — save to notes
