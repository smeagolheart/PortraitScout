import java.awt.Dimension;
import java.awt.image.BufferedImage;
import java.io.File;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.zip.CRC32;
import java.nio.file.Path;
import java.nio.file.Paths;

import javax.imageio.ImageIO;

import org.infinity.resource.Profile;
import org.infinity.resource.ResourceFactory;
import org.infinity.resource.cre.CreResource;
import org.infinity.resource.cre.decoder.SpriteDecoder;
import org.infinity.resource.cre.decoder.util.Sequence;
import org.infinity.resource.graphics.BamDecoder;
import org.infinity.resource.key.BufferedResourceEntry;
import org.infinity.resource.key.ResourceEntry;

/**
 * Portrait Scout helper: draws one still frame of a creature's in-game sprite, in its own colors, with
 * Near Infinity's own decoder (the Animation tab's first cycle of the standing sequence).
 *
 * java -cp NearInfinity.jar;spriteshot.jar SpriteShot <game folder> <output folder> <cycle> CRE1 [CRE2 ...]
 * (cycle -1: every direction, as NAME_c0.png, NAME_c1.png ..., besides NAME.png)
 *
 * Writes <output folder>/<CRE>.png for each creature it can draw and prints "OK CRE" or "FAIL CRE: reason".
 */
public class SpriteShot {
  public static void main(String[] args) throws Exception {
    if (args.length < 4) {
      System.err.println("usage: SpriteShot <game folder> <output folder> <cycle> CRE...");
      System.exit(2);
    }
    Class.forName("org.infinity.AppOption");        // the static parts first, while there is no menu bar yet:
    Class.forName("org.infinity.NearInfinity");     // they look for one, and a half-built one breaks them
    if (!org.infinity.gui.menu.BrowserMenuBar.isInstantiated()) {
      new org.infinity.gui.menu.BrowserMenuBar();   // Near Infinity reads its settings through it; never shown
    }
    Path key = Paths.get(args[0], "chitin.key");
    if (!Profile.openGame(key, "Game")) {
      System.out.println("FAIL game: could not open " + key);
      System.exit(1);
    }
    File out = new File(args[1]);
    out.mkdirs();
    int cycle = Integer.parseInt(args[2]);
    Map<Integer, int[]> sets = randomSets();
    for (int i = 3; i < args.length; i++) {
      String name = args[i].toUpperCase();
      try {
        ResourceEntry e = ResourceFactory.getResourceEntry(name + ".CRE");
        if (e == null) {
          System.out.println("FAIL " + name + ": no such creature");
          continue;
        }
        CreResource cre = new CreResource(rolled(e, name, sets));
        SpriteDecoder dec = SpriteDecoder.importSprite(cre);
        Sequence seq = null;
        for (Sequence s : new Sequence[] { Sequence.STAND, Sequence.STAND2, Sequence.STAND3, Sequence.STANCE,
            Sequence.STANCE2, Sequence.STAND_EMERGED }) {
          if (dec.isSequenceAvailable(s)) {
            seq = s;
            break;
          }
        }
        if (seq == null) {
          for (Sequence s : Sequence.values()) {
            if (s != Sequence.NONE && dec.isSequenceAvailable(s)) {
              seq = s;
              break;
            }
          }
        }
        if (seq == null || !dec.loadSequence(seq)) {
          System.out.println("FAIL " + name + ": no animation to draw");
          continue;
        }
        SpriteDecoder.SpriteBamControl c = dec.createControl();
        c.setMode(BamDecoder.BamControl.Mode.SHARED);
        c.setSharedPerCycle(false);
        Dimension d = c.getSharedDimension();
        int first = cycle < 0 ? 0 : Math.max(0, Math.min(c.cycleCount() - 1, cycle));
        int last = cycle < 0 ? c.cycleCount() - 1 : first;   // -1: every direction, NAME_c0.png ... NAME_cN.png
        for (int cy = first; cy <= last; cy++) {
          c.cycleSet(cy);
          c.cycleSetFrameIndex(0);
          BufferedImage img = new BufferedImage(Math.max(1, d.width), Math.max(1, d.height),
              BufferedImage.TYPE_INT_ARGB);
          c.cycleGetFrame(img);
          img = trim(img);
          if (cy == first) {
            ImageIO.write(img, "png", new File(out, name + ".png"));
          }
          if (cycle < 0) {
            ImageIO.write(img, "png", new File(out, name + "_c" + cy + ".png"));
          }
        }
        System.out.println("OK " + name + (cycle < 0 ? " " + c.cycleCount() : ""));
      } catch (Throwable t) {
        System.out.println("FAIL " + name + ": " + t);
      }
    }
    System.exit(0);
  }

  private static final String[] SLOTS = { "metal", "minor", "major", "skin", "leather", "armor", "hair" };

  /** RANDCOLR.2DA: a color byte of 200 or more -> the palette rows the game rolls one of. */
  private static Map<Integer, int[]> randomSets() {
    Map<Integer, int[]> out = new HashMap<>();
    try {
      ResourceEntry e = ResourceFactory.getResourceEntry("RANDCOLR.2DA");
      if (e == null) return out;
      ByteBuffer b = e.getResourceBuffer();
      byte[] a = new byte[b.remaining()];
      b.get(a);
      List<String[]> rows = new ArrayList<>();
      for (String ln : new String(a, "ISO-8859-1").split("\\r?\\n")) {
        if (!ln.trim().isEmpty()) rows.add(ln.trim().split("\\s+"));
      }
      if (rows.size() < 4) return out;
      String[] vals = rows.get(3);
      for (int i = 1; i < vals.length; i++) {
        if (!vals[i].matches("\\d+")) continue;
        List<Integer> opts = new ArrayList<>();
        for (int r = 4; r < rows.size(); r++) {
          String[] row = rows.get(r);
          if (row.length > i && row[i].matches("\\d+")) opts.add(Integer.parseInt(row[i]));
        }
        if (!opts.isEmpty()) {
          int[] o = new int[opts.size()];
          for (int k = 0; k < o.length; k++) o[k] = opts.get(k);
          out.put(Integer.parseInt(vals[i]), o);
        }
      }
    } catch (Throwable t) {
      // no table: the colors stay as the file has them
    }
    return out;
  }

  /** The creature with its random colors rolled the way Portrait Scout describes it (CRC32 of "NAME:slot"),
   * so the sprite and the portrait show the same one. */
  private static ResourceEntry rolled(ResourceEntry e, String name, Map<Integer, int[]> sets) throws Exception {
    ByteBuffer src = e.getResourceBuffer();
    byte[] a = new byte[src.remaining()];
    src.get(a);
    boolean changed = false;
    for (int i = 0; i < 7 && 0x2c + i < a.length; i++) {
      int[] opts = sets.get(a[0x2c + i] & 0xff);
      if (opts == null) continue;
      CRC32 c = new CRC32();
      c.update((name + ":" + SLOTS[i]).getBytes("ISO-8859-1"));
      int o = opts[(int) (c.getValue() % opts.length)];
      for (int depth = 1; depth < 8; depth++) {     // a roll of 200 or more is itself a set: rolled again
        int[] nxt = sets.get(o);
        if (nxt == null || nxt.length == 0) break;
        CRC32 c2 = new CRC32();
        c2.update((name + ":" + SLOTS[i] + ":" + depth).getBytes("ISO-8859-1"));
        o = nxt[(int) (c2.getValue() % nxt.length)];
      }
      a[0x2c + i] = (byte) o;
      changed = true;
    }
    if (!changed) return e;
    return new BufferedResourceEntry(ByteBuffer.wrap(a).order(ByteOrder.LITTLE_ENDIAN), name + ".CRE");
  }

  /** The picture cut to what is drawn, with a little room around it. */
  private static BufferedImage trim(BufferedImage img) {
    int w = img.getWidth(), h = img.getHeight();
    int x0 = w, y0 = h, x1 = -1, y1 = -1;
    for (int y = 0; y < h; y++) {
      for (int x = 0; x < w; x++) {
        if ((img.getRGB(x, y) >>> 24) != 0) {
          if (x < x0) x0 = x;
          if (x > x1) x1 = x;
          if (y < y0) y0 = y;
          if (y > y1) y1 = y;
        }
      }
    }
    if (x1 < 0) {
      return img;
    }
    int m = 4;
    x0 = Math.max(0, x0 - m);
    y0 = Math.max(0, y0 - m);
    x1 = Math.min(w - 1, x1 + m);
    y1 = Math.min(h - 1, y1 + m);
    return img.getSubimage(x0, y0, x1 - x0 + 1, y1 - y0 + 1);
  }
}
