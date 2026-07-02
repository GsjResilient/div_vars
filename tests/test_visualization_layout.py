import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from partition_aig_cutpoints import (  # noqa: E402
    build_graph,
    build_key_visualization_data,
    build_visualization_data,
    compute_cuts,
    parse_aig,
    write_visualization,
)


class VisualizationLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.aig = parse_aig(ROOT / "test_15_TOP32_72.aig")
        (
            cls.neighbors,
            cls.directed_edges,
            _metis_to_aig,
            cls.aig_to_metis,
            cls.excluded_output_variables,
        ) = build_graph(cls.aig)

    def build_partition_data(self, parts):
        part = [-1] + [vertex % parts for vertex in range(1, len(self.neighbors))]
        report = compute_cuts(
            self.aig,
            part,
            self.neighbors,
            self.directed_edges,
            self.aig_to_metis,
            self.excluded_output_variables,
        )
        return build_visualization_data(self.aig, part, self.aig_to_metis, report)

    def test_two_way_layout_separates_partitions(self):
        data = self.build_partition_data(2)
        layout = data["partitionLayout"]
        self.assertEqual(data["version"], 2)
        self.assertEqual(layout["partitionIds"], [0, 1])

        groups = {group["partition"]: group for group in layout["groups"]}
        self.assertLess(groups[0]["maxX"], groups[1]["minX"])
        for node in data["nodes"]:
            self.assertIn("partitionX", node)
            self.assertIn("partitionY", node)
            if node["partition"] in groups:
                group = groups[node["partition"]]
                self.assertGreaterEqual(node["partitionX"], group["minX"])
                self.assertLessEqual(node["partitionX"], group["maxX"])
                self.assertGreaterEqual(node["partitionY"], group["minY"] + 300)
            else:
                neutral = layout["neutralGroup"]
                self.assertGreaterEqual(node["partitionY"], neutral["minY"])
                self.assertLessEqual(node["partitionY"], neutral["maxY"])

    def test_non_two_way_layout_is_disabled(self):
        data = self.build_partition_data(3)
        self.assertIsNone(data["partitionLayout"])

    def test_key_file_mode_does_not_enable_partition_layout(self):
        data = build_key_visualization_data(self.aig, [2, 3, 4], Path("keys.txt"))
        self.assertEqual(data["mode"], "key-file")
        self.assertIsNone(data["partitionLayout"])

    def test_rendered_html_contains_partition_controls(self):
        data = self.build_partition_data(2)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "visualization.html"
            write_visualization(output, data)
            html = output.read_text(encoding="utf-8")
        self.assertIn('id="layout-control"', html)
        self.assertIn('id="partition-visibility"', html)
        self.assertIn('data-layout="partition"', html)


if __name__ == "__main__":
    unittest.main()
