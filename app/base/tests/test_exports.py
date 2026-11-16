"""
`base.exports`: the CSV writer every report goes through.

The formula-injection rule lives here once, so it is tested here once on its
own terms; each report then has a test that its own user-typed cells actually
pass through it.
"""

import csv
import io
from decimal import Decimal

import pytest

from base.exports import cents_as_dollars, csv_response, neutralise


class TestNeutralise:
    @pytest.mark.parametrize("trigger", ["=", "+", "-", "@", "\t", "\r"])
    def test_text_that_would_start_a_formula_is_prefixed(self, trigger):
        assert neutralise(f"{trigger}cmd|' /C calc'!A0") == f"'{trigger}cmd|' /C calc'!A0"

    def test_a_typed_value_that_looks_negative_is_text(self):
        """A person typed it, so it is text, however much it looks like a number."""
        assert neutralise("-10") == "'-10"
        assert neutralise("-2+3") == "'-2+3"

    @pytest.mark.parametrize("value", ["Dana Hale", "Smith-Jones", "a=b", "3 + 4", " =1+1", ""])
    def test_ordinary_text_is_left_alone(self, value):
        assert neutralise(value) == value

    @pytest.mark.parametrize("value", [0, 7200, -5, Decimal("-25.00"), Decimal("150.00")])
    def test_a_number_this_code_computed_stays_a_number(self, value):
        """
        A number cannot carry a formula, and as text it could not be summed.
        A negative one included: only what was typed is suspect.
        """
        assert neutralise(value) is value

    def test_none_is_an_empty_cell(self):
        assert neutralise(None) == ""

    def test_a_bool_is_refused(self):
        with pytest.raises(TypeError):
            neutralise(True)


class TestCentsAsDollars:
    @pytest.mark.parametrize(
        ("cents", "dollars"),
        [
            (0, "0.00"),
            (5, "0.05"),
            (7, "0.07"),
            (15000, "150.00"),
            (123456789, "1234567.89"),
            (-2500, "-25.00"),
        ],
    )
    def test_the_decimal_point_moves_and_nothing_else(self, cents, dollars):
        assert str(cents_as_dollars(cents)) == dollars

    def test_it_is_never_a_float(self):
        assert isinstance(cents_as_dollars(7), Decimal)


class TestCsvResponse:
    def _rows(self, response):
        return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))

    def test_header_then_rows_as_a_download(self):
        response = csv_response("a.csv", ["name", "total_usd"], [["Dana", cents_as_dollars(15000)]])

        assert response["Content-Type"] == "text/csv; charset=utf-8"
        assert response["Content-Disposition"] == 'attachment; filename="a.csv"'
        assert self._rows(response) == [["name", "total_usd"], ["Dana", "150.00"]]

    def test_every_text_cell_is_neutralised(self):
        response = csv_response("a.csv", ["name"], [['=HYPERLINK("http://x","y")'], ["-10"]])

        assert self._rows(response)[1:] == [['\'=HYPERLINK("http://x","y")'], ["'-10"]]

    def test_commas_quotes_and_newlines_stay_inside_their_cell(self):
        awkward = 'Hale, "Dana"\nsecond line'

        response = csv_response("a.csv", ["name", "n"], [[awkward, 1]])

        assert self._rows(response)[1] == [awkward, "1"]

    @pytest.mark.parametrize("delimiter", [";", "\t"])
    def test_a_typed_value_is_one_cell_whatever_the_reader_splits_on(self, delimiter):
        """
        The file is comma-separated, but a spreadsheet set to split on
        semicolons or tabs does not ask. Unquoted, the name below becomes
        two cells there and the second is a formula nothing looked at.
        """
        name = f"Hale{delimiter}=1+1"

        response = csv_response("a.csv", ["name"], [[name]])
        text = response.content.decode("utf-8-sig")
        as_that_reader_sees_it = list(csv.reader(io.StringIO(text), delimiter=delimiter))

        assert as_that_reader_sees_it[1] == [name]
        assert not any(cell.startswith("=") for row in as_that_reader_sees_it for cell in row)

    def test_text_is_quoted_and_numbers_are_bare(self):
        response = csv_response("a.csv", ["name", "n", "usd"], [["Dana", 2, cents_as_dollars(-5)]])

        assert response.content.decode("utf-8-sig") == ('"name","n","usd"\r\n"Dana",2,-0.05\r\n')

    def test_it_opens_with_a_bom_so_excel_reads_utf8(self):
        response = csv_response("a.csv", ["name"], [["Renée"]])

        assert response.content.startswith(b"\xef\xbb\xbf")
        assert "Renée".encode() in response.content
